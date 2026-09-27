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
            return f"ℹ️ Таблицы не найдены. Возможно, на этой неделе нет записей или страница загружается через JS.\nСсылка: {target_url}"

    except Exception as e:
        return f"⚠️ Ошибка соединения: {e}"
