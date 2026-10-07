"""
Главный пайплайн: парсинг площадок по списку запросов, анализ вакансий через LLM,
генерация писем и сохранение результатов. Внедрена постоянная память проверенных ссылок.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from openai import APIError, APITimeoutError, AsyncOpenAI, RateLimitError
from pydantic import BaseModel, Field, ValidationError, field_validator

from parsers import HHParser, SuperJobParser, HabrParser, GetMatchParser, RemoteJobParser
from excel_exporter import ExcelExporter

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("resume_matcher")

ROOT = Path(__file__).resolve().parent
RESUME_PATH = ROOT / "resume_template.txt"
PROMPT_LETTER_PATH = ROOT / "prompt_cover_letter.txt"
# ФАЙЛ ПАМЯТИ: сюда скрипт будет записывать все увиденные ссылки навсегда
CHECKED_URLS_PATH = ROOT / "checked_urls.txt"

SEARCH_QUERIES = [
    "AI Developer",
    "AI Engineer",
    "LLM",
    "RAG",
    "Prompt",
    "Вайбкодер",
    "ИИ-инженер",
    "AI-автоматизация"
]


class Settings(BaseModel):
    openai_api_key: str
    openai_base_url: str
    llm_model: str = "gpt-4o-mini"
    llm_smart_model: str = "gpt-4o"


def load_settings() -> Settings:
    try:
        return Settings(
            openai_api_key=os.environ["OPENAI_API_KEY"],
            openai_base_url=os.environ["OPENAI_BASE_URL"],
            llm_model=os.getenv("LLM_MODEL", "gpt-4o-mini"),
            llm_smart_model=os.getenv("LLM_SMART_MODEL", "gpt-4o"),
        )
    except KeyError as exc:
        logger.error("Не задана обязательная переменная окружения: %s", exc.args[0])
        sys.exit(1)
    except ValidationError as exc:
        logger.error("Ошибка конфигурации:\n%s", exc)
        sys.exit(1)


def read_text_file(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.error("Не удалось прочитать файл %s: %s", path, exc)
        raise
    if not text:
        logger.error("Файл пустой: %s", path)
        raise ValueError(f"Файл пустой: {path}")
    return text


class VacancyAnalysis(BaseModel):
    match_percentage: int = Field(ge=0, le=100)
    verdict: Literal["Подходит", "Не подходит"]
    pros: list[str]
    missing_skills: list[str]
    short_summary: str

    @field_validator("pros", "missing_skills")
    @classmethod
    def non_empty_items(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item and item.strip()]


def build_system_prompt(resume: str) -> str:
    """Формирует системный промпт для первого агента (анализ соответствия резюме)."""
    return f"""Ты — строгий и технически грамотный IT-рекрутер. Твоя задача — оценить, является ли текст вакансией, и насколько она подходит кандидату.

Кандидат — AI-native разработчик (Product Builder) с уникальным гибридным профилем: 1 год интенсивной разработки AI/LLM-решений (Python, LangGraph, RAG, Docker) + 15 лет управления B2B-продуктами, маркетингом и аналитикой. Проживает в Омске (UTC+6).

Полный текст резюме кандидата:
{resume}

ПРАВИЛА ОЦЕНКИ (СТРОГО СОБЛЮДАТЬ):
1. ФЕЙК-КОНТРОЛЬ (БАЗОВЫЙ ФИЛЬТР - КРИТИЧЕСКИ ВАЖНО):
Сначала убедись, что перед тобой реальное предложение о работе (найм в штат или на проект). Если текст является рекламой образовательных курсов (например, Stepik), подборкой Telegram-каналов, анонсом вебинара, новостью про IT, обзором рынка (например, зарплат CUDA-инженеров) или мемом — НЕМЕДЛЕННО ставь match_percentage: 0 и verdict: "Не подходит". В таком случае в short_summary просто напиши: "Это не вакансия, а реклама/информационный пост".

2. Опыт работы (Отмена фильтра по годам):
Категорически запрещено занижать match_percentage или ставить «Не подходит», если требуют 1–3 года коммерческой разработки. Требование «опыт 3+ года» трактуй как «умение автономно закрывать задачи бизнеса от идеи до продакшена». 15 лет B2B-бэкграунда кандидата полностью это перекрывают.

3. Стек и Вайбкодинг (Гибкость):
Ядро кандидата: Python, FastAPI, LangGraph, RAG, ChromaDB, Docker, LLM API.
Если вакансия требует дополнительные языки или фреймворки (JavaScript, Vue, Django, React, C++) для вспомогательных задач, НЕ считай это критичным минусом. Кандидат использует подход "вайбкодинга" (Cursor, Gemini) для портирования кода.

4. Локация и формат работы (Жесткий фильтр):
- Полная удаленка = 100% совпадение.
- Офис/гибрид в Омске = 100% совпадение.
- Офис/гибрид в других городах = СНИЖАЙ match_percentage на 30 баллов, ЕСЛИ в тексте вакансии прямо не сказано, что возможна удаленка.

5. Целевое направление (Strict AI vs Classic ML):
- Идеально (85-100%): Agentic AI, RAG, интеграция LLM, промпт-инжиниринг, автоматизация бизнес-процессов (n8n), Tool/Function Calling. 
- Не подходит (<60%): Чистый классический Data Science (обучение моделей с нуля, мат. статистика) или классический Backend без AI.

ФОРМАТ ВЫВОДА:
Верни ТОЛЬКО валидный JSON со следующими полями без markdown, без форматирования кода и без пояснений:
{{
"match_percentage": <целое число от 0 до 100>,
"verdict": <строго "Подходит", если match_percentage >= 75, иначе "Не подходит">,
"pros": <массив строк: конкретные сильные совпадения (стек, продуктовый опыт, домен, удаленка)>,
"missing_skills": <массив строк: критичные требования вакансии, которых нет в резюме>,
"short_summary": <строка: ровно два предложения на русском языке, объясняющие вердикт>
}}
"""


def extract_json(raw: str) -> str:
    text = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    if fenced:
        return fenced.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text


class LLMAnalyzer:
    def __init__(self, settings: Settings, resume: str) -> None:
        self._client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url.rstrip("/"),
            timeout=60.0,
            max_retries=2,
        )
        self._model = settings.llm_model
        self._smart_model = settings.llm_smart_model
        self._resume = resume
        self._system_prompt = build_system_prompt(resume)

    async def analyze(self, vacancy_text: str) -> VacancyAnalysis:
        messages = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": vacancy_text[:12000]},
        ]
        try:
            try:
                completion = await self._client.beta.chat.completions.parse(
                    model=self._model,
                    messages=messages,
                    response_format=VacancyAnalysis,
                    temperature=0.2,
                )
                parsed = completion.choices[0].message.parsed
                if parsed is not None:
                    return parsed
            except Exception as parse_exc:
                logger.debug("Structured parse недоступен, fallback на JSON: %s", parse_exc)

            completion = await self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                temperature=0.2,
                response_format={"type": "json_object"},
            )
            content = completion.choices[0].message.content or ""
            payload = json.loads(extract_json(content))
            return VacancyAnalysis.model_validate(payload)
        except Exception as exc:
            logger.error("Ошибка LLM: %s", exc)
            raise

    async def generate_cover_letter(self, vacancy_text: str, analysis: VacancyAnalysis) -> str:
        if not PROMPT_LETTER_PATH.exists():
            return "Ошибка: файл промпта для письма не найден."
        
        sys_prompt = read_text_file(PROMPT_LETTER_PATH)
        pros_text = "\n".join(f"- {p}" for p in analysis.pros)
        user_content = (
            f"ТЕКСТ ВАКАНСИИ:\n{vacancy_text[:8000]}\n\n"
            f"РЕЗЮМЕ КАНДИДАТА:\n{self._resume}\n\n"
            f"ОСНОВНЫЕ СОВПАДЕНИЯ:\n{pros_text}"
        )

        try:
            completion = await self._client.chat.completions.create(
                model=self._smart_model,
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_content}
                ],
                temperature=0.7,
            )
            return completion.choices[0].message.content or ""
        except Exception as exc:
            logger.error("Ошибка генерации сопроводительного письма: %s", exc)
            return "Ошибка генерации."

    async def close(self) -> None:
        await self._client.close()


async def run() -> None:
    settings = load_settings()
    resume = read_text_file(RESUME_PATH)
    analyzer = LLMAnalyzer(settings, resume)
    exporter = ExcelExporter()
    
    parsers = [
        # HHParser(),
        SuperJobParser(),
        HabrParser(),
        GetMatchParser(),
        RemoteJobParser()
    ]
    
    # 1. ЗАГРУЖАЕМ ИСТОРИЮ (ПАМЯТЬ)
    processed_urls = set()
    if CHECKED_URLS_PATH.exists():
        processed_urls = set(CHECKED_URLS_PATH.read_text(encoding="utf-8").splitlines())
        logger.info("Загружено %s уже проверенных ссылок из памяти.", len(processed_urls))
    
    try:
        for query in SEARCH_QUERIES:
            logger.info("="*50)
            logger.info("Начинаю парсинг площадок по запросу: '%s'", query)
            logger.info("="*50)
            
            for parser in parsers:
                logger.info("--- Собираю данные с ресурса: %s ---", parser.source)
                vacancies = await parser.fetch_vacancies(query)
                
                for vac in vacancies:
                    vac_url = vac.get("url", "")
                    
                    # 2. ПРОВЕРЯЕМ ПАМЯТЬ. Если видели — моментально пропускаем!
                    if vac_url in processed_urls:
                        continue
                        
                    if not vac.get("description"):
                        continue
                    
                    # 3. ЗАПИСЫВАЕМ В ПАМЯТЬ сразу, чтобы не забыть
                    if vac_url:
                        processed_urls.add(vac_url)
                        with open(CHECKED_URLS_PATH, "a", encoding="utf-8") as f:
                            f.write(vac_url + "\n")
                    
                    try:
                        analysis = await analyzer.analyze(vac["description"])
                    except Exception:
                        continue 
                    
                    logger.info("[%s%%] %s | %s", analysis.match_percentage, analysis.verdict, vac["title"])
                    
                    if analysis.verdict == "Подходит" or analysis.match_percentage >= 75:
                        logger.info("Вакансия подошла! Генерирую письмо умной моделью...")
                        letter = await analyzer.generate_cover_letter(vac["description"], analysis)
                        
                        exporter.add_vacancy(
                            source=vac["source"],
                            title=vac["title"],
                            url=vac_url,
                            match_percentage=analysis.match_percentage,
                            pros=analysis.pros,
                            cons=analysis.missing_skills,
                            summary=analysis.short_summary,
                            cover_letter=letter
                        )
                    
                    await asyncio.sleep(0.5)
                    
        logger.info("Конвейер завершил работу! Результаты сохранены в suitable_vacancies.xlsx")       
 
    finally:
        for parser in parsers:
            await parser.close()
        await asyncio.sleep(0.25)
        await analyzer.close()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Остановка по Ctrl+C")
        sys.exit(130)
    except Exception:
        sys.exit(1)