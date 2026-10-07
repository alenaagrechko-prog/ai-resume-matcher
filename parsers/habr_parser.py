"""Парсер вакансий с career.habr.com (HTML парсинг)."""

import asyncio
import logging
from bs4 import BeautifulSoup
from .base_parser import BaseJobParser, VacancyDict, strip_html

logger = logging.getLogger("resume_matcher")

class HabrParser(BaseJobParser):
    source = "Habr Карьера"
    base_url = "https://career.habr.com"

    def _headers(self) -> dict[str, str]:
        headers = super()._headers()
        # Максимальная маскировка под реальный браузер (Chrome)
        headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": "https://career.habr.com/"
        })
        return headers

    async def fetch_vacancies(self, query: str) -> list[VacancyDict]:
        results: list[VacancyDict] = []
        
        for page in range(self.max_pages):
            url = f"{self.base_url}/vacancies"
            
            # Убрали 'type=all', который мог вызывать 404. 
            # Добавляем параметр страницы, только если это не первая страница (иногда ?page=1 тоже дает 404).
            # Хабр любит подмешивать мусор, поэтому оборачиваем запрос в кавычки для строгого поиска
            strict_query = f'"{query}"' if " " in query else query
            params = {"q": strict_query, "sort": "date"}
            if page > 0:
                params["page"] = page + 1
            
            logger.info("Habr: Обрабатываю страницу %s. URL: %s с параметрами %s", page + 1, url, params)
            
            session = await self._session_or_create()
            try:
                async with session.get(url, params=params, headers=self._headers()) as response:
                    if response.status == 404:
                        logger.warning("Habr: Вакансии по запросу '%s' не найдены (или сработала защита 404).", query)
                        break
                    elif response.status >= 400:
                        logger.error("Habr: Ошибка сети HTTP %s", response.status)
                        break
                    html = await response.text()
            except Exception as exc:
                logger.error("Habr: Исключение сети: %s", exc)
                break

            soup = BeautifulSoup(html, "html.parser")
            cards = soup.find_all("div", class_="vacancy-card")
            
            if not cards:
                logger.warning("Habr: Карточки не найдены на стр %s по запросу '%s'.", page + 1, query)
                break

            for card in cards:
                title_tag = card.find("div", class_="vacancy-card__title")
                if not title_tag:
                    continue
                
                link_tag = title_tag.find("a")
                if not link_tag or not link_tag.get("href"):
                    continue
                    
                title = link_tag.text.strip()
                link = self.base_url + link_tag["href"]
                
                company_tag = card.find("div", class_="vacancy-card__company-title")
                company = company_tag.text.strip() if company_tag else "Не указана"
                
                await self._pause()
                description = ""
                try:
                    async with session.get(link, headers=self._headers()) as resp:
                        if resp.status == 200:
                            vac_html = await resp.text()
                            vac_soup = BeautifulSoup(vac_html, "html.parser")
                            desc_tag = vac_soup.find("div", class_="vacancy-description__text")
                            if desc_tag:
                                description = strip_html(desc_tag.get_text(separator="\n"))
                except Exception as e:
                    logger.debug("Habr: Ошибка описания %s: %s", link, e)

                if description:
                    results.append(self._item(title=title, company=company, url=link, description=description, source=self.source))

            logger.info("Habr: Собрано %s вакансий со страницы %s", len(results), page + 1)
            await self._pause()

        return results