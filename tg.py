"""
Парсер Telegram-каналов через веб-зеркала (t.me/s/...).
Берет список каналов напрямую из файла .env (TELEGRAM_CHANNELS),
интегрирован с общей памятью и ИИ-агентами из main.py.
"""

import asyncio
import logging
import os
import re
import sys
from pathlib import Path

import aiohttp
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# Импортируем готовые классы и функции из твоего main.py
from main import (
    LLMAnalyzer,
    load_settings,
    read_text_file,
    RESUME_PATH,
    CHECKED_URLS_PATH
)
from excel_exporter import ExcelExporter

# Принудительно загружаем переменные из .env
load_dotenv()

logger = logging.getLogger("tg_matcher")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

def get_tg_channels_from_env() -> list[str]:
    """
    Достает строку с каналами из .env и очищает их от мусора
    (убирает https://, t.me/, @), оставляя только чистые юзернеймы.
    """
    raw_str = os.getenv("TELEGRAM_CHANNELS", "")
    if not raw_str:
        logger.warning("Переменная TELEGRAM_CHANNELS пуста или не задана в .env!")
        return []
    
    cleaned_channels = []
    # Разбиваем по запятой
    for raw_name in raw_str.split(","):
        raw_name = raw_name.strip()
        if not raw_name:
            continue
        
        # Регуляркой отрезаем префиксы ссылок
        name = re.sub(r"https?://", "", raw_name)
        name = re.sub(r"t\.me/", "", name)
        name = name.replace("@", "")
        # Убираем возможные пробелы и слэши по краям
        name = name.strip(" /")
        
        if name:
            cleaned_channels.append(name)
            
    return cleaned_channels


class TelegramWebParser:
    def __init__(self, channels: list[str]):
        self.channels = channels
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }

    async def fetch_channel_posts(self, channel: str) -> list[dict]:
        """Собирает последние посты из публичного веб-зеркала канала."""
        url = f"https://t.me/s/{channel}"
        posts = []
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=self.headers, timeout=15) as response:
                    if response.status != 200:
                        logger.error("Канал @%s недоступен (HTTP %s). Возможно, он приватный.", channel, response.status)
                        return posts
                    
                    html = await response.text()
        except Exception as e:
            logger.error("Ошибка при подключении к @%s: %s", channel, e)
            return posts

        soup = BeautifulSoup(html, "html.parser")
        
        # Находим все блоки сообщений на веб-странице
        message_blocks = soup.find_all("div", class_="tgme_widget_message")
        
        for block in message_blocks:
            text_div = block.find("div", class_="tgme_widget_message_text")
            if not text_div:
                continue
                
            # Извлекаем ссылку на конкретный пост (например, t.me/python_rabota/1234)
            post_link = ""
            date_a = block.find("a", class_="tgme_widget_message_date")
            if date_a and date_a.get("href"):
                post_link = date_a["href"]

            # Вытаскиваем текст с сохранением переносов строк
            text = text_div.get_text(separator="\n", strip=True)
            
            # Если пост слишком короткий — это явно не вакансия
            if len(text) < 200:
                continue
                
            # Берем первые 50 символов как "Заголовок" вакансии для Excel
            title = text.split("\n")[0][:50] + "..."

            posts.append({
                "source": f"TG: @{channel}",
                "title": title,
                "url": post_link,
                "description": text
            })

        logger.info("TG @%s: Собрано %s потенциальных постов", channel, len(posts))
        return posts


async def run_tg() -> None:
    # 1. Загружаем каналы из .env
    channels = get_tg_channels_from_env()
    if not channels:
        logger.error("Нет каналов для парсинга. Добавь TELEGRAM_CHANNELS в .env и перезапусти скрипт.")
        return
        
    logger.info("Успешно загружено %s каналов из .env", len(channels))

    settings = load_settings()
    resume = read_text_file(RESUME_PATH)
    analyzer = LLMAnalyzer(settings, resume)
    exporter = ExcelExporter()
    parser = TelegramWebParser(channels)
    
    # 2. ЗАГРУЖАЕМ ПАМЯТЬ
    processed_urls = set()
    if CHECKED_URLS_PATH.exists():
        processed_urls = set(CHECKED_URLS_PATH.read_text(encoding="utf-8").splitlines())
        logger.info("Загружено %s ссылок из памяти.", len(processed_urls))
    
    try:
        for channel in parser.channels:
            logger.info("="*50)
            logger.info("Читаю канал: @%s", channel)
            logger.info("="*50)
            
            posts = await parser.fetch_channel_posts(channel)
            
            for post in posts:
                post_url = post["url"]
                
                if post_url in processed_urls:
                    continue
                
                if post_url:
                    processed_urls.add(post_url)
                    with open(CHECKED_URLS_PATH, "a", encoding="utf-8") as f:
                        f.write(post_url + "\n")
                
                try:
                    analysis = await analyzer.analyze(post["description"])
                except Exception:
                    continue 
                
                logger.info("[%s%%] %s | %s", analysis.match_percentage, analysis.verdict, post["url"])
                
                if analysis.verdict == "Подходит" or analysis.match_percentage >= 75:
                    logger.info("Пост похож на вакансию! Генерирую сопроводительное письмо...")
                    letter = await analyzer.generate_cover_letter(post["description"], analysis)
                    
                    exporter.add_vacancy(
                        source=post["source"],
                        title=post["title"],
                        url=post_url,
                        match_percentage=analysis.match_percentage,
                        pros=analysis.pros,
                        cons=analysis.missing_skills,
                        summary=analysis.short_summary,
                        cover_letter=letter
                    )
                
                await asyncio.sleep(0.5) 
                
        logger.info("TG-парсинг завершен! Подходящие вакансии добавлены в suitable_vacancies.xlsx")
        
    finally:
        await analyzer.close()

if __name__ == "__main__":
    try:
        asyncio.run(run_tg())
    except KeyboardInterrupt:
        logger.info("Остановка по Ctrl+C")
        sys.exit(130)
    except Exception:
        sys.exit(1)