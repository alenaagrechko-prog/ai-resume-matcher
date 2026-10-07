#base_parser.py

"""Базовый интерфейс парсеров вакансий."""

from __future__ import annotations

import asyncio
import logging
import re
from abc import ABC, abstractmethod
from typing import Any

import aiohttp

logger = logging.getLogger("resume_matcher")

VacancyDict = dict[str, str]

DEFAULT_HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "ru-RU,ru;q=0.9",
}


def strip_html(text: str | None) -> str:
    """Убирает HTML-теги из описания вакансии."""
    if not text:
        return ""
    cleaned = re.sub(r"(?i)<br\s*/?>", "\n", text)
    cleaned = re.sub(r"(?i)</p>", "\n", cleaned)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = re.sub(r"&nbsp;", " ", cleaned)
    cleaned = re.sub(r"&amp;", "&", cleaned)
    cleaned = re.sub(r"&lt;", "<", cleaned)
    cleaned = re.sub(r"&gt;", ">", cleaned)
    cleaned = re.sub(r"&quot;", '"', cleaned)
    return re.sub(r"[ \t]+\n", "\n", re.sub(r"[ \t]{2,}", " ", cleaned)).strip()


class BaseJobParser(ABC):
    """Абстрактный парсер: единый контракт для HH, Работа России и др."""

    source: str = "unknown"
    max_pages: int = 3
    per_page: int = 20
    request_delay: float = 1.2
    request_timeout: float = 25.0

    def __init__(
        self,
        session: aiohttp.ClientSession | None = None,
        *,
        max_pages: int | None = None,
        request_delay: float | None = None,
    ) -> None:
        self._session = session
        self._owns_session = session is None
        if max_pages is not None:
            self.max_pages = max_pages
        if request_delay is not None:
            self.request_delay = request_delay

    @abstractmethod
    async def fetch_vacancies(self, query: str) -> list[VacancyDict]:
        """
        Ищет вакансии по запросу.

        Каждый элемент списка — словарь с ключами:
        title, company, url, description, source.
        """

    def _headers(self) -> dict[str, str]:
        return dict(DEFAULT_HEADERS)

    async def _session_or_create(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.request_timeout)
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                headers=self._headers(),
            )
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()

    async def _get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        session = await self._session_or_create()
        try:
            async with session.get(url, params=params, headers=self._headers()) as response:
                if response.status == 429:
                    retry_after = float(response.headers.get("Retry-After", "5"))
                    logger.warning(
                        "%s: лимит запросов (429), пауза %.1f с",
                        self.source,
                        retry_after,
                    )
                    await asyncio.sleep(retry_after)
                    return None
                if response.status >= 400:
                    body = await response.text()
                    logger.error(
                        "%s: HTTP %s для %s: %s",
                        self.source,
                        response.status,
                        url,
                        body[:300],
                    )
                    return None
                return await response.json(content_type=None)
        except asyncio.TimeoutError:
            logger.error("%s: таймаут запроса %s", self.source, url)
            return None
        except aiohttp.ClientError as exc:
            logger.error("%s: сетевая ошибка %s: %s", self.source, url, exc)
            return None
        except ValueError as exc:
            logger.error("%s: ответ не JSON (%s): %s", self.source, url, exc)
            return None

    async def _pause(self) -> None:
        if self.request_delay > 0:
            await asyncio.sleep(self.request_delay)

    @staticmethod
    def _item(
        *,
        title: str,
        company: str,
        url: str,
        description: str,
        source: str,
    ) -> VacancyDict:
        return {
            "title": (title or "").strip(),
            "company": (company or "").strip(),
            "url": (url or "").strip(),
            "description": (description or "").strip(),
            "source": source,
        }
