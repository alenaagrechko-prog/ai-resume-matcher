"""Парсер вакансий с remote-job.ru (Расширенный HTML парсинг)."""

import asyncio
import logging
from bs4 import BeautifulSoup
from .base_parser import BaseJobParser, VacancyDict, strip_html

logger = logging.getLogger("resume_matcher")

class RemoteJobParser(BaseJobParser):
    source = "remote-job.ru"
    base_url = "https://remote-job.ru"

    def _headers(self) -> dict[str, str]:
        headers = super()._headers()
        headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        return headers

    async def fetch_vacancies(self, query: str) -> list[VacancyDict]:
        results: list[VacancyDict] = []
        
        for page in range(self.max_pages):
            url = f"{self.base_url}/search"
            params = {"search_query": query, "page": page + 1}
            
            logger.info("RemoteJob: Обрабатываю страницу %s. URL: %s с параметрами %s", page + 1, url, params)
            
            session = await self._session_or_create()
            try:
                async with session.get(url, params=params, headers=self._headers()) as response:
                    if response.status >= 400:
                        logger.error("RemoteJob: Ошибка сети HTTP %s", response.status)
                        break
                    html = await response.text()
            except Exception as exc:
                logger.error("RemoteJob: Исключение сети: %s", exc)
                break

            soup = BeautifulSoup(html, "html.parser")
            
            # Фоллбэк: жестко ищем любые ссылки, которые ведут на /vacancy/
            links = soup.find_all("a", href=True)
            vacancy_links = [a for a in links if "/vacancy/" in a["href"]]
            
            if not vacancy_links:
                logger.warning("RemoteJob: Карточки не найдены на стр %s по запросу '%s'.", page + 1, query)
                break
                
            added_urls = set()
            for a in vacancy_links:
                title = a.text.strip()
                if not title:
                    continue
                    
                link = a["href"]
                if link.startswith("/"):
                    link = self.base_url + link
                    
                if link in added_urls:
                    continue
                added_urls.add(link)
                
                await self._pause()
                description = ""
                try:
                    async with session.get(link, headers=self._headers()) as resp:
                        if resp.status == 200:
                            vac_html = await resp.text()
                            vac_soup = BeautifulSoup(vac_html, "html.parser")
                            main_content = vac_soup.find("main") or vac_soup.find("div", class_="content") or vac_soup.body
                            if main_content:
                                description = strip_html(main_content.get_text(separator="\n"))
                except Exception as e:
                    logger.debug("RemoteJob: Ошибка описания %s: %s", link, e)

                if description:
                    results.append(self._item(title=title, company="Удаленная компания", url=link, description=description, source=self.source))

            logger.info("RemoteJob: Собрано %s вакансий со страницы %s", len(results), page + 1)
            await self._pause()

        return results