"""Парсер вакансий с Superjob через официальное API."""

import asyncio
import logging
import os
from .base_parser import BaseJobParser, VacancyDict, strip_html

logger = logging.getLogger("resume_matcher")

class SuperJobParser(BaseJobParser):
    source = "Superjob"

    def _headers(self) -> dict[str, str]:
        headers = super()._headers()
        # Забираем ключ из .env и передаем в правильном заголовке
        secret_key = os.getenv("SUPERJOB_SECRET_KEY")
        if secret_key:
            headers["X-Api-App-Id"] = secret_key
        else:
            logger.warning("SUPERJOB_SECRET_KEY не найден в переменных окружения!")
        return headers

    async def fetch_vacancies(self, query: str) -> list[VacancyDict]:
        results: list[VacancyDict] = []
        url = "https://api.superjob.ru/2.0/vacancies/"

        for page in range(self.max_pages):
            params = {
                "keyword": query,
                "page": page,
                "count": self.per_page,
            }
            
            data = await self._get_json(url, params)
            if not data or "objects" not in data:
                break

            items = data["objects"]
            if not items:
                break

            for item in items:
                # В Superjob полное описание лежит в vacancyRichText или candidat
                desc = item.get("vacancyRichText") or item.get("candidat") or ""
                
                results.append(
                    self._item(
                        title=item.get("profession", ""),
                        company=item.get("firm_name", ""),
                        url=item.get("link", ""),
                        description=strip_html(desc),
                        source=self.source,
                    )
                )

            logger.info("Superjob: Собрано %s вакансий со страницы %s", len(items), page + 1)
            await self._pause()

        return results