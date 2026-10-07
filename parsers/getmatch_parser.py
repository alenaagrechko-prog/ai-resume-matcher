"""Парсер вакансий с getmatch.ru (API парсинг)."""

import asyncio
import logging
from .base_parser import BaseJobParser, VacancyDict, strip_html

logger = logging.getLogger("resume_matcher")

class GetMatchParser(BaseJobParser):
    source = "Getmatch"
    base_url = "https://getmatch.ru/api/offers/search"

    def _headers(self) -> dict[str, str]:
        headers = super()._headers()
        headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        return headers

    async def fetch_vacancies(self, query: str) -> list[VacancyDict]:
        results: list[VacancyDict] = []
        
        for page in range(self.max_pages):
            params = {
                "q": query,
                "page": page + 1,
                "per_page": self.per_page
            }
            
            logger.info("Getmatch: Обрабатываю страницу %s. Запрос: %s", page + 1, params)
            
            session = await self._session_or_create()
            try:
                async with session.get(self.base_url, params=params, headers=self._headers()) as response:
                    # ХАК ДЛЯ GETMATCH: Они отдают 404, если вакансий 0. Это не сбой сайта.
                    if response.status == 404:
                        logger.warning("Getmatch: Вакансии по запросу '%s' не найдены (API вернул 404).", query)
                        break
                    elif response.status >= 400:
                        logger.error("Getmatch: Ошибка HTTP %s", response.status)
                        break
                        
                    data = await response.json()
            except Exception as exc:
                logger.error("Getmatch: Исключение сети: %s", exc)
                break

            offers = data.get("offers", [])
            if not offers:
                logger.warning("Getmatch: Пустой список на странице %s по запросу '%s'.", page + 1, query)
                break

            for offer in offers:
                title = offer.get("position", "")
                company = offer.get("company", {}).get("name", "")
                
                offer_id = offer.get("id", "")
                link = f"https://getmatch.ru/vacancies/{offer_id}" if offer_id else ""
                
                req = offer.get("requirements", "")
                desc = offer.get("description", "")
                description = strip_html(str(req) + "\n" + str(desc))

                if not description or not link:
                    continue

                results.append(
                    self._item(title=title, company=company, url=link, description=description, source=self.source)
                )

            logger.info("Getmatch: Собрано %s вакансий со страницы %s", len(results), page + 1)
            await self._pause()

        return results