import os
import json
import logging
import asyncio
import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ==========================================
# Настройка логирования
# ==========================================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Токен Telegram-бота (из переменных окружения или укажите вручную)
BOT_TOKEN = os.getenv("BOT_TOKEN", "ВАШ_ТОКЕН_БОТА_ЗДЕСЬ")
SESSIONS_FILE = "user_sessions.json"

# ==========================================
# Сохранение и загрузка сессий из JSON
# ==========================================
def load_sessions_from_disk():
    if os.path.exists(SESSIONS_FILE):
        try:
            with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                # JSON ключи делает строками, преобразуем обратно в int (user_id)
                return {int(k): v for k, v in data.items()}
        except Exception as e:
            logger.error(f"Ошибка чтения {SESSIONS_FILE}: {e}")
    return {}

def save_sessions_to_disk():
    try:
        data_to_save = {}
        for uid, session in user_sessions.items():
            data_to_save[uid] = {
                'cookie': session.get('cookie'),
                'last_cookie': session.get('last_cookie'),
                'interval': session.get('interval', 1),
                'active_date': session.get('active_date'),
                'status': None  # Не сохраняем временные статусы диалога
            }
        with open(SESSIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(data_to_save, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Ошибка сохранения {SESSIONS_FILE}: {e}")

# Инициализируем хранилище
user_sessions = load_sessions_from_disk()

# ==========================================
# Клавиатуры (UI)
# ==========================================
def get_main_keyboard(has_cookie: bool):
    if has_cookie:
        buttons = [
            [InlineKeyboardButton("📊 Получить дневник", callback_data="get_diary")],
            [InlineKeyboardButton("🔄 Обновить сессию Госуслуг", callback_data="login_gosuslugi")],
            [InlineKeyboardButton("⚙️ Ввести Cookie вручную", callback_data="input_cookie")],
            [InlineKeyboardButton("❌ Выйти из аккаунта", callback_data="logout")]
        ]
    else:
        buttons = [
            [InlineKeyboardButton("🔑 Войти через Госуслуги", callback_data="login_gosuslugi")],
            [InlineKeyboardButton("⚙️ Ввести Cookie вручную", callback_data="input_cookie")]
        ]
    return InlineKeyboardMarkup(buttons)

def get_cancel_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚫 Отмена", callback_data="cancel")]
    ])

def get_logout_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 На главную", callback_data="main_menu")]
    ])

# ==========================================
# Запросы к Дневник.ру через requests
# ==========================================
def fetch_dnevnik_page(cookie_str: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Cookie": cookie_str
    }
    url = "https://schools.dnevnik.ru/v2/feed"
    response = requests.get(url, headers=headers, timeout=15)
    return response.text

# ==========================================
# Управление ресурсами Playwright
# ==========================================
async def close_playwright_session(user_id: int):
    session = user_sessions.get(user_id)
    if session:
        browser = session.get('browser')
        pw = session.get('playwright')
        if browser:
            try:
                await browser.close()
            except Exception as e:
                logger.error(f"Ошибка закрытия browser: {e}")
        if pw:
            try:
                await pw.stop()
            except Exception as e:
                logger.error(f"Ошибка остановки playwright: {e}")
        session['browser'] = None
        session['page'] = None
        session['playwright'] = None

# ==========================================
# Логика работы с Госуслугами (Playwright)
# ==========================================
async def process_gosuslugi_login(user_id: int, login: str, password: str, status_msg, context: ContextTypes.DEFAULT_TYPE):
    session = user_sessions.get(user_id)
    if not session:
        return

    # Директория для персонального профиля браузера
    user_profile_dir = os.path.join(os.getcwd(), "browser_profiles", str(user_id))
    os.makedirs(user_profile_dir, exist_ok=True)

    try:
        pw = await async_playwright().start()
        
        # Используем постоянный профиль контекста (сохраняет куки/токены Госуслуг)
        context_bw = await pw.chromium.launch_persistent_context(
            user_data_dir=user_profile_dir,
            headless=True,
            channel="chromium",
            args=[
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-blink-features=AutomationControlled'
            ],
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        
        page = context_bw.pages[0] if context_bw.pages else await context_bw.new_page()
        session.update({'playwright': pw, 'browser': context_bw, 'page': page})

        await status_msg.edit_text("🔗 Перехожу на страницу авторизации Дневник.ру...", reply_markup=get_cancel_keyboard())
        
        # Заходим на страницу авторизации
        await page.goto("https://login.dnevnik.ru/login/gosuslugi", timeout=60000, wait_until="domcontentloaded")

        # Если мы УЖЕ вошли ранее в этом профиле, сразу забираем куки!
        if "login" not in page.url.lower():
            await status_msg.edit_text("⚡️ Найдена сохраненная сессия! Загружаю данные...")
            await finalize_gosuslugi_login(user_id, status_msg)
            return

        await status_msg.edit_text("✍️ Ввожу логин и пароль...", reply_markup=get_cancel_keyboard())

        # Безопасное ожидание селекторов ввода
        login_input = await page.wait_for_selector('input[id="login"], input[name="login"]', timeout=20000)
        if login_input:
            await login_input.fill(login)
        
        password_input = await page.wait_for_selector('input[id="password"], input[name="password"]', timeout=10000)
        if password_input:
            await password_input.fill(password)

        submit_btn = await page.wait_for_selector('button[type="submit"]', timeout=10000)
        if submit_btn:
            await submit_btn.click()

        await asyncio.sleep(5)

        # Проверка на запрос SMS / 2FA
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
        logger.error(f"Ошибка входа через Госуслуги для {user_id}: {e}")
        await status_msg.edit_text(
            f"❌ Ошибка авторизации Госуслуг: {e}\n\nПопробуйте ещё раз или воспользуйтесь прямым вводом Cookie.",
            reply_markup=get_logout_keyboard()
        )
        await close_playwright_session(user_id)

async def process_sms_code(user_id: int, sms_code: str, status_msg):
    session = user_sessions.get(user_id)
    page = session.get('page') if session else None

    if not page or page.is_closed():
        await status_msg.edit_text("❌ Сессия авторизации истекла. Попробуйте зайти снова.", reply_markup=get_logout_keyboard())
        await close_playwright_session(user_id)
        return

    try:
        await status_msg.edit_text("📩 Проверяю SMS-код...", reply_markup=get_cancel_keyboard())
        
        sms_input = await page.wait_for_selector('input[name="code"], input[id="otp"]', timeout=10000)
        if sms_input:
            await sms_input.fill(sms_code)
            
        submit_btn = await page.wait_for_selector('button[type="submit"]', timeout=10000)
        if submit_btn:
            await submit_btn.click()

        await asyncio.sleep(5)
        await finalize_gosuslugi_login(user_id, status_msg)

    except Exception as e:
        logger.error(f"Ошибка при вводе SMS: {e}")
        await status_msg.edit_text(f"❌ Ошибка обработки SMS-кода: {e}", reply_markup=get_logout_keyboard())
        await close_playwright_session(user_id)

async def finalize_gosuslugi_login(user_id: int, status_msg):
    session = user_sessions.get(user_id)
    context_bw = session.get('browser') if session else None

    if not context_bw:
        await status_msg.edit_text("❌ Ошибка контекста браузера.", reply_markup=get_logout_keyboard())
        return

    try:
        # Дожидаемся редиректа на Дневник.ру
        page = session.get('page')
        if page:
            await page.goto("https://schools.dnevnik.ru/", timeout=30000, wait_until="domcontentloaded")

        # Извлекаем Cookie
        cookies = await context_bw.cookies()
        cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])

        if cookie_str and len(cookie_str) > 20:
            session['cookie'] = cookie_str
            session['last_cookie'] = cookie_str
            session['status'] = None
            
            # Сохраняем сессию на диск
            save_sessions_to_disk()

            await status_msg.edit_text(
                "✅ **Успешная авторизация!**\n\nСессия сохранена. Теперь вы можете запрашивать данные дневника.",
                parse_mode="Markdown",
                reply_markup=get_main_keyboard(has_cookie=True)
            )
        else:
            await status_msg.edit_text("❌ Не удалось извлечь куки авторизации.", reply_markup=get_logout_keyboard())

    except Exception as e:
        logger.error(f"Ошибка финализации: {e}")
        await status_msg.edit_text(f"❌ Ошибка получения сессии: {e}", reply_markup=get_logout_keyboard())
    finally:
        await close_playwright_session(user_id)

# ==========================================
# Автоматическое фоновое обновление сессий
# ==========================================
async def auto_refresh_cookies_task():
    """Каждые 12 часов тихо обновляем куки без участия пользователя"""
    while True:
        await asyncio.sleep(43200) # 12 часов
        logger.info("Запуск фонового обновления куки...")
        for user_id, session in list(user_sessions.items()):
            user_profile_dir = os.path.join(os.getcwd(), "browser_profiles", str(user_id))
            if os.path.exists(user_profile_dir):
                try:
                    pw = await async_playwright().start()
                    context_bw = await pw.chromium.launch_persistent_context(
                        user_data_dir=user_profile_dir,
                        headless=True,
                        args=['--no-sandbox', '--disable-setuid-sandbox']
                    )
                    page = context_bw.pages[0] if context_bw.pages else await context_bw.new_page()
                    await page.goto("https://schools.dnevnik.ru/", timeout=30000)
                    
                    cookies = await context_bw.cookies()
                    cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
                    
                    if cookie_str and len(cookie_str) > 20:
                        session['cookie'] = cookie_str
                        session['last_cookie'] = cookie_str
                        save_sessions_to_disk()
                        logger.info(f"Куки для пользователя {user_id} успешно обновлены")
                    
                    await context_bw.close()
                    await pw.stop()
                except Exception as e:
                    logger.error(f"Ошибка фонового обновления для {user_id}: {e}")

# ==========================================
# Telegram Хэндлеры
# ==========================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in user_sessions:
        user_sessions[user_id] = {'cookie': None, 'status': None}
        save_sessions_to_disk()

    session = user_sessions[user_id]
    has_cookie = bool(session.get('cookie'))

    text = "👋 **Привет! Я бот для работы с Дневник.ру.**\n\n Выберите действие:"
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=get_main_keyboard(has_cookie))

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if user_id not in user_sessions:
        user_sessions[user_id] = {'cookie': None, 'status': None}

    session = user_sessions[user_id]
    data = query.data

    if data == "main_menu":
        session['status'] = None
        has_cookie = bool(session.get('cookie'))
        await query.edit_message_text("Главное меню:", reply_markup=get_main_keyboard(has_cookie))

    elif data == "login_gosuslugi":
        session['status'] = 'WAITING_GOSUSLUGI_CREDENTIALS'
        text = "🔐 **Авторизация через Госуслуги**\n\nПришлите логин и пароль в одном сообщении через пробел:\n`79991234567 ваш_пароль`"
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=get_cancel_keyboard())

    elif data == "input_cookie":
        session['status'] = 'WAITING_COOKIE'
        text = "⚙️ **Ввод Cookie вручную**\n\nОтправьте строку Cookie из вашего браузера:"
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=get_cancel_keyboard())

    elif data == "get_diary":
        cookie = session.get('cookie')
        if not cookie:
            await query.edit_message_text("❌ Cookie не найдены. Выполните вход.", reply_markup=get_main_keyboard(False))
            return

        status_msg = await query.edit_message_text("⏳ Загружаю дневник...")
        try:
            html = fetch_dnevnik_page(cookie)
            soup = BeautifulSoup(html, 'html.parser')
            
            # Простейший парсинг названия/заголовка для проверки
            title = soup.title.string if soup.title else "Страница дневника"
            await status_msg.edit_text(f"📊 **Результат:**\nСтраница успешно получена! `{title.strip()}`", parse_mode="Markdown", reply_markup=get_main_keyboard(True))
        except Exception as e:
            await status_msg.edit_text(f"❌ Ошибка запроса к Дневник.ру: {e}", reply_markup=get_main_keyboard(True))

    elif data == "logout":
        user_sessions[user_id] = {'cookie': None, 'status': None}
        save_sessions_to_disk()
        await query.edit_message_text("🚪 Вы успешно вышли из системы.", reply_markup=get_main_keyboard(False))

    elif data == "cancel":
        session['status'] = None
        await close_playwright_session(user_id)
        has_cookie = bool(session.get('cookie'))
        await query.edit_message_text("Действие отменено.", reply_markup=get_main_keyboard(has_cookie))

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    if user_id not in user_sessions:
        user_sessions[user_id] = {'cookie': None, 'status': None}

    session = user_sessions[user_id]
    status = session.get('status')

    if status == 'WAITING_GOSUSLUGI_CREDENTIALS':
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await update.message.reply_text("❌ Неверный формат! Введите логин и пароль через пробел.")
            return

        login, password = parts[0], parts[1]
        session['status'] = 'PROCESSING_GOSUSLUGI'
        status_msg = await update.message.reply_text("⏳ Запускаю браузер...")
        
        # Фоновый запуск авторизации
        asyncio.create_task(process_gosuslugi_login(user_id, login, password, status_msg, context))

    elif status == 'WAITING_SMS':
        session['status'] = 'PROCESSING_SMS'
        status_msg = await update.message.reply_text("⏳ Отправляю SMS-код...")
        asyncio.create_task(process_sms_code(user_id, text, status_msg))

    elif status == 'WAITING_COOKIE':
        session['cookie'] = text
        session['last_cookie'] = text
        session['status'] = None
        save_sessions_to_disk()
        await update.message.reply_text("✅ Cookie успешно сохранены!", reply_markup=get_main_keyboard(True))

    else:
        has_cookie = bool(session.get('cookie'))
        await update.message.reply_text("Используйте кнопки меню для управления:", reply_markup=get_main_keyboard(has_cookie))

# ==========================================
# Запуск приложения
# ==========================================
def main():
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Запуск фонового обновления сессий
    loop = asyncio.get_event_loop()
    loop.create_task(auto_refresh_cookies_task())

    logger.info("Бот запущен!")
    application.run_polling()

if __name__ == "__main__":
    main()
