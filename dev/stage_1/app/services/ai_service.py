import asyncio
import json
import logging
import re
import typing

import httpx
from sqlalchemy import select

from app.config import settings
from app.models.legislation import QueryExpansionCache

logger = logging.getLogger(__name__)

_expansion_cache: dict[str, str] = {}
_MAX_RETRIES = 10


def _build_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if settings.openai_api_key and settings.openai_api_key != "not-needed":
        headers["Authorization"] = f"Bearer {settings.openai_api_key}"
    return headers


def _build_payload(
    model: str = "auto",
    messages: list | None = None,
    temperature: float = 0.1,
    max_tokens: int = 1024,
    provider: str = "auto",
) -> dict:
    payload: dict = {
        "provider": provider,
        "model": model,
        "metadata": {"client_id": settings.llm_client_id},
        "messages": messages or [],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    return payload

_discovered_models: list[str] | None = None
_DISCOVERY_LOCK = asyncio.Lock()
_CHAT_EXCLUDE_KEYWORDS = frozenset([
    'whisper', 'embedding', 'guard', 'orpheus', 'tts',
    'robotics', 'clip', 'lyria', 'er-', 'safeguard',
    'rerank', 'safety', 'search',
])
_PRIORITY_DISCOVERED = [
    'qwen/qwen3-32b',
    'models/gemini-2.5-flash',
    'meta-llama/llama-4-scout-17b-16e-instruct',
    'llama-3.3-70b-versatile',
    'llama-3.1-8b-instant',
]


def _is_chat_model(model_id: str) -> bool:
    lower = model_id.lower()
    for kw in _CHAT_EXCLUDE_KEYWORDS:
        if kw in lower:
            return False
    return True


async def _discover_models() -> list[str]:
    global _discovered_models
    if _discovered_models is not None:
        return _discovered_models

    async with _DISCOVERY_LOCK:
        if _discovered_models is not None:
            return _discovered_models

        try:
            async with httpx.AsyncClient(timeout=10, verify=False) as client:
                resp = await client.get(
                    f"{settings.openai_base_url}/models",
                    headers=_build_headers(),
                )
                resp.raise_for_status()
                data = resp.json()
                all_models = [m["id"] for m in data.get("data", []) if isinstance(m, dict)]

                prioritized = [m for m in _PRIORITY_DISCOVERED if m in all_models]
                rest = [m for m in all_models if m not in prioritized and _is_chat_model(m)]
                _discovered_models = prioritized + rest
                logger.warning("Discovered %d chat models (%d prioritized)",
                               len(_discovered_models), len(prioritized))
                return _discovered_models
        except Exception as e:
            logger.warning("Model discovery failed: %s", e)
            return []


NEW_EXPANSION_PROMPT = """Ты — юридический аналитик. Определи тип ситуации и сгенерируй поисковые запросы.

Ситуация: {query}

Верни ТОЛЬКО JSON:
{{
  "situation_type": "ДТП|трудовой_спор|наследство|жилищный_спор|договор|административное_право|уголовное_право|семейное_право|налоговое_право|земельное_право|исполнительное_производство|иное",
  "search_queries": [
    "конкретные термины описывающие ситуацию",
    "синонимы и юридические эквиваленты"
  ],
  "confidence": 0-100
}}

Правила:
- 3-5 поисковых запросов, каждый 2-4 слова
- Используй термины, которые МОГУТ БЫТЬ В ТЕКСТЕ закона или статьи
- Не выдумывай номера статей и конкретные отсылки к законам
- Не используй общие слова вроде ответственность, обязанность, право, нарушение — они есть в каждом законе и не помогают искать
- Вместо "административная ответственность" напиши "правила дорожного движения" или "аварийная обстановка"
- Если ситуация про ДТП: пиши "дорожное движение транспортное средство авария"
- Если про магазин: пиши "товар возврат недостаток продавец потребитель"
- Если про работу: пиши "трудовой договор зарплата увольнение работник"
- confidence = насколько ты уверен, что запросы помогут найти relevant статьи
- Не добавляй пояснений, только JSON"""


def _parse_expansion_response(raw: str) -> tuple[str | None, int]:
    text = raw.strip()
    if not text:
        return None, 0

    json_match = re.search(r"\{.*\}", text, re.DOTALL)
    if json_match:
        try:
            obj = json.loads(json_match.group())
            confidence = min(max(int(obj.get("confidence", 0)), 0), 100)
            queries = obj.get("search_queries", [])
            if isinstance(queries, list) and queries:
                # Filter out queries with RF-specific article references
                filtered = [q for q in queries if isinstance(q, str) and not re.search(r'\bстатья\s+\d+', q, re.IGNORECASE)]
                if not filtered:
                    filtered = queries
                best = max(filtered, key=lambda q: len(q.split())) if len(filtered) > 1 else filtered[0]
                if isinstance(best, str) and best.strip():
                    words = best.strip().split()
                    if 1 <= len(words) <= 10:
                        return " ".join(words[:10]), confidence
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    words = re.findall(r"[а-яёa-z]+", text.lower())
    words = [w for w in words if len(w) > 2]
    if 1 <= len(words) <= 10:
        return " ".join(words[:10]), 30

    return None, 0


async def _try_expansion_model(query: str, model: str = "auto") -> tuple[str | None, int]:
    prompt = NEW_EXPANSION_PROMPT.format(query=query)

    try:
        async with httpx.AsyncClient(timeout=settings.openai_timeout, verify=False) as client:
            payload = _build_payload(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=512,
            )

            resp = await client.post(
                f"{settings.openai_base_url}/chat/completions",
                json=payload,
                headers=_build_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            msg = data["choices"][0]["message"]

            content = (msg.get("content") or "").strip()
            parsed, conf = _parse_expansion_response(content)
            if parsed:
                return parsed, conf

            reasoning = (msg.get("reasoning") or "").strip()
            parsed, conf = _parse_expansion_response(reasoning)
            if parsed:
                return parsed, conf

            logger.warning("Expansion model %s no parseable output", model)
    except Exception as e:
        logger.warning("Expansion model %s failed (%s)", model, e)

    return None, 0


async def _call_expansion(query: str) -> tuple[str | None, int]:
    if settings.llm_model == "auto":
        result, conf = await _try_expansion_model(query)
        return (result, conf) if result else (None, 0)

    best_result = None
    best_conf = 0

    result, conf = await _try_expansion_model(query)
    if result:
        if conf >= best_conf:
            best_result, best_conf = result, conf

    models_to_try = [settings.llm_model] + _FALLBACK_MODELS
    for model in models_to_try:
        result, conf = await _try_expansion_model(query, model)
        if result and conf > best_conf:
            best_result, best_conf = result, conf

    discovered = await _discover_models()
    already_tried = set(models_to_try)
    for model in discovered:
        if model in already_tried:
            continue
        already_tried.add(model)
        result, conf = await _try_expansion_model(query, model)
        if result and conf > best_conf:
            best_result, best_conf = result, conf
            logger.warning("Query expansion succeeded via discovered model: %s", model)

    return best_result, best_conf


async def expand_query_for_search(query: str, db) -> tuple[str | None, int]:
    cached = _expansion_cache.get(query)
    if cached:
        logger.info("Query expansion cache HIT (memory): %r -> %r", query, cached)
        return cached, 0

    result = await db.execute(
        select(QueryExpansionCache).where(QueryExpansionCache.original_query == query)
    )
    row = result.scalar_one_or_none()
    if row:
        _expansion_cache[query] = row.expanded_query
        logger.info("Query expansion cache HIT (db): %r -> %r", query, row.expanded_query)
        return row.expanded_query, getattr(row, 'confidence', 0)

    last_error = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            expanded, conf = await _call_expansion(query)
            if expanded:
                _expansion_cache[query] = expanded
                db.add(QueryExpansionCache(original_query=query, expanded_query=expanded, confidence=conf))
                await db.commit()
                logger.info("Query expanded (attempt %d): %r -> %r (confidence=%d)", attempt, query, expanded, conf)
                return expanded, conf

            last_error = "empty or unparseable response"
            logger.warning("Query expansion attempt %d: %s for %r", attempt, last_error, query)

        except httpx.TimeoutException as e:
            last_error = f"timeout: {e}"
            logger.warning("Query expansion attempt %d: %s for %r", attempt, last_error, query)

        except httpx.HTTPStatusError as e:
            last_error = f"HTTP {e.response.status_code}: {e}"
            logger.warning("Query expansion attempt %d: %s for %r", attempt, last_error, query)

        except Exception as e:
            last_error = str(e)
            logger.exception("Query expansion attempt %d error for %r: %s", attempt, query, last_error)

        if attempt < _MAX_RETRIES:
            await asyncio.sleep(attempt * 1.5)

    logger.error("Query expansion FAILED after %d attempts: %s — %r", _MAX_RETRIES, last_error, query)
    return None, 0

async def select_law_article_llm(
    situation: str,
    law_list: list[tuple[int, str, str | None, str]],
) -> dict:
    laws_text = "\n".join(
        f"{i+1}. {title} (№{num}) [{cat}]" if num else f"{i+1}. {title} [{cat}]"
        for i, (_, title, num, cat) in enumerate(law_list)
    )

    prompt = f"""Ситуация пользователя: {situation}

Список доступных законов РК (выбери ТОЛЬКО из этого списка):
{laws_text}"""

    models_to_try = [settings.llm_model] + _FALLBACK_MODELS

    result = None
    async with httpx.AsyncClient(timeout=settings.openai_timeout, verify=False) as client:
        payload = _build_payload(
            messages=[
                {"role": "system", "content": SELECTION_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            max_tokens=512,
        )
        try:
            resp = await client.post(
                f"{settings.openai_base_url}/chat/completions",
                json=payload,
                headers=_build_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            json_match = re.search(r"\{.*\}", content, re.DOTALL)
            if json_match:
                obj = json.loads(json_match.group())
                law_title = obj.get("law_title") or ""
                article_number = str(obj.get("article_number") or "")
                confidence = int(obj.get("confidence", 0))
                reasoning = obj.get("reasoning", "")
                result = {
                    "law_title": law_title.strip(),
                    "article_number": article_number.strip(),
                    "confidence": min(max(confidence, 0), 100),
                    "reasoning": reasoning.strip(),
                }
        except Exception as e:
            logger.warning("Law selection auto/auto attempt failed: %s", e)

    if result or settings.llm_model == "auto":
        return result or {"law_title": "", "article_number": "", "confidence": 0, "reasoning": ""}

    for model in models_to_try:
        for attempt in range(2):
            try:
                payload = _build_payload(
                    model=model,
                    messages=[
                        {"role": "system", "content": SELECTION_SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.1,
                    max_tokens=512,
                )

                resp = await client.post(
                    f"{settings.openai_base_url}/chat/completions",
                    json=payload,
                    headers=_build_headers(),
                )
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"]

                json_match = re.search(r"\{.*\}", content, re.DOTALL)
                if json_match:
                    obj = json.loads(json_match.group())
                    law_title = obj.get("law_title") or ""
                    article_number = str(obj.get("article_number") or "")
                    confidence = int(obj.get("confidence", 0))
                    reasoning = obj.get("reasoning", "")
                    return {
                        "law_title": law_title.strip(),
                        "article_number": article_number.strip(),
                        "confidence": min(max(confidence, 0), 100),
                        "reasoning": reasoning.strip(),
                    }
            except Exception as e:
                logger.warning("Law selection model %s attempt %d: %s", model, attempt + 1, e)
                await asyncio.sleep(0.5)

    return {"law_title": "", "article_number": "", "confidence": 0, "reasoning": ""}


async def generate_control_question(
    situation: str,
    law_title: str,
    article_number: str,
    article_content: str,
) -> tuple[str, int]:
    prompt = f"""Ситуация пользователя: {situation}

Выбранный закон: {law_title}
Статья №{article_number}
Содержание статьи: {article_content[:1500]}"""

    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=settings.openai_timeout, verify=False) as client:
                payload = _build_payload(
                    messages=[
                        {"role": "system", "content": CONTROL_QUESTION_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.3,
                    max_tokens=256,
                )
                resp = await client.post(
                    f"{settings.openai_base_url}/chat/completions",
                    json=payload,
                    headers=_build_headers(),
                )
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"]

                json_match = re.search(r"\{.*\}", content, re.DOTALL)
                if json_match:
                    obj = json.loads(json_match.group())
                    q = obj.get("control_question", "")
                    conf = min(max(int(obj.get("confidence", 0)), 0), 100)
                    if q:
                        return q.strip(), conf
        except Exception as e:
            logger.warning("Control question attempt %d failed: %s", attempt + 1, e)
            if attempt < 2:
                await asyncio.sleep(1)

    return "", 0


EXTRACT_CONDITIONS_PROMPT = """Ты — юридический аналитик РК. Извлеки условия применения данной статьи закона.

Условие применения — это конкретное обстоятельство, которое должно быть (или отсутствовать), чтобы норма права применялась.
Примеры: «работник отсутствовал более 3 часов», «акт об отсутствии составлен», «нет уважительной причины», «договор заключён в письменной форме».

Статья: {article_title}
Текст статьи: {article_content}

Верни ТОЛЬКО JSON:
{{
  "conditions": [
    {{
      "condition_text": "текст условия",
      "condition_type": "duration|document|circumstance|status|action|other",
      "is_required": true
    }}
  ],
  "confidence": 0-100
}}

Если в статье нет явных условий применения, верни {{"conditions": [], "confidence": 0}}"""


COMPLETENESS_CHECK_PROMPT = """Ты — юрист РК. Проверь, достаточно ли деталей в описании ситуации для применения найденных статей закона.

Ситуация клиента: {situation}

Найденные статьи с условиями их применения:
{articles_with_conditions}

Тип клиента: {client_type}
  forms — пользователь в веб/мобильном интерфейсе
  api — программный вызов (агент)

Задача:
1. Для каждой статьи определи, какие её условия явно описаны или подразумеваются в ситуации, а какие не упомянуты.
2. Оцени полноту описания (completeness 0-100):
   - 100 = все условия всех статей описаны в ситуации или очевидно подразумеваются
   - 0 = ни одного условия не описано
3. Оцени свою уверенность (confidence 0-100)
4. Составь suggestion:
   - Если completeness < 100: исходная ситуация + уточняющие вопросы в конце
   - Если completeness = 100: поставь suggestion = "-" (минус, признак пустого suggestion)
5. Составь instruction:
   - Если completeness < 100: команда для {client_type}-клиента, как отправить ответ
   - Если completeness = 100: instruction = "-"

Правила:
- Если completeness < 100, НЕ давай юридический анализ, только вопросы
- Если все условия уже описаны — completeness = 100
- Не выдумывай условия, которых нет в статьях

Верни ТОЛЬКО JSON без пояснений:
{{
  "completeness": 0-100,
  "confidence": 0-100,
  "clarifying_questions": ["вопрос1", "вопрос2"],
  "suggestion": "исходная ситуация. вопрос1? вопрос2?",
  "instruction": "Отправьте полное описание ситуации одним сообщением, дополнив его ответами на уточняющие вопросы."
}}"""


def _is_consistent_completeness(completeness: int, suggestion: str) -> bool:
    if completeness < 100 and suggestion == "-":
        return False
    if completeness >= 100 and suggestion != "-":
        return False
    return True


CallableProgress = typing.Callable[[str], typing.Awaitable[None]]


async def check_completeness(
    situation: str, articles_text: str, client_type: str = "forms",
    progress_callback: CallableProgress | None = None,
) -> dict:
    prompt = COMPLETENESS_CHECK_PROMPT.format(
        situation=situation,
        articles_with_conditions=articles_text,
        client_type=client_type,
    )

    async def _warn(msg: str) -> None:
        if progress_callback:
            await progress_callback(msg)

    async with httpx.AsyncClient(timeout=settings.openai_timeout, verify=False) as client:
        payload = _build_payload(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=1024,
        )
        try:
            resp = await client.post(
                f"{settings.openai_base_url}/chat/completions",
                json=payload,
                headers=_build_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            json_match = re.search(r"\{.*\}", content, re.DOTALL)
            if json_match:
                obj = json.loads(json_match.group())
                result = {
                    "completeness": min(max(int(obj.get("completeness", 0)), 0), 100),
                    "confidence": min(max(int(obj.get("confidence", 0)), 0), 100),
                    "clarifying_questions": obj.get("clarifying_questions", []) if isinstance(obj.get("clarifying_questions"), list) else [],
                    "suggestion": str(obj.get("suggestion", situation)),
                    "instruction": str(obj.get("instruction", "Отправьте полное описание ситуации одним сообщением, дополнив его ответами на уточняющие вопросы.")),
                }
                if _is_consistent_completeness(result["completeness"], result["suggestion"]):
                    return result
                await _warn(f"⚠ Неконсистентный ответ LLM: completeness={result['completeness']}, suggestion={result['suggestion']!r} — повтор...")
        except Exception as e:
            logger.warning("check_completeness auto/auto attempt failed: %s", e)

    if settings.llm_model == "auto":
        await _warn("✗ auto-режим не дал ответа, использую резервный")
        return {
            "completeness": 100,
            "confidence": 50,
            "clarifying_questions": [],
            "suggestion": "-",
            "instruction": "-",
        }

    models_to_try = [settings.llm_model] + _FALLBACK_MODELS
    for model in models_to_try:
        for attempt in range(2):
            try:
                if attempt > 0 or model != models_to_try[0]:
                    await _warn(f"⚠ Модель {model} (попытка {attempt + 1})...")

                async with httpx.AsyncClient(timeout=settings.openai_timeout, verify=False) as client:
                    payload = _build_payload(
                        model=model,
                        messages=[{"role": "user", "content": prompt}],
                        temperature=0.1,
                        max_tokens=1024,
                    )

                    resp = await client.post(
                        f"{settings.openai_base_url}/chat/completions",
                        json=payload,
                        headers=_build_headers(),
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    content = data["choices"][0]["message"]["content"]

                    json_match = re.search(r"\{.*\}", content, re.DOTALL)
                    if json_match:
                        obj = json.loads(json_match.group())
                        completeness = int(obj.get("completeness", 0))
                        confidence = int(obj.get("confidence", 0))
                        questions = obj.get("clarifying_questions", [])
                        suggestion = obj.get("suggestion", situation)
                        instruction = obj.get("instruction", "Отправьте полное описание ситуации одним сообщением, дополнив его ответами на уточняющие вопросы.")

                        result = {
                            "completeness": min(max(completeness, 0), 100),
                            "confidence": min(max(confidence, 0), 100),
                            "clarifying_questions": questions if isinstance(questions, list) else [],
                            "suggestion": str(suggestion),
                            "instruction": str(instruction),
                        }

                        if _is_consistent_completeness(result["completeness"], result["suggestion"]):
                            return result
                        await _warn(
                            f"⚠ Неконсистентный ответ LLM: completeness={result['completeness']}, "
                            f"suggestion={result['suggestion']!r} — повтор..."
                        )
            except Exception as e:
                logger.warning("check_completeness %s attempt %d: %s", model, attempt + 1, e)
                await _warn(f"⚠ Ошибка {model} (попытка {attempt + 1}): {e}")
                await asyncio.sleep(0.5)

    await _warn("✗ Все попытки исчерпаны, использую резервный ответ")
    return {
        "completeness": 100,
        "confidence": 50,
        "clarifying_questions": [],
        "suggestion": "-",
        "instruction": "-",
    }


async def extract_conditions_from_article(article_title: str, article_content: str) -> tuple[list[dict], int]:
    prompt = EXTRACT_CONDITIONS_PROMPT.format(
        article_title=article_title or "",
        article_content=article_content[:2000] if article_content else "",
    )

    async with httpx.AsyncClient(timeout=settings.openai_timeout, verify=False) as client:
        payload = _build_payload(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=1024,
        )
        try:
            resp = await client.post(
                f"{settings.openai_base_url}/chat/completions",
                json=payload,
                headers=_build_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            json_match = re.search(r"\{.*\}", content, re.DOTALL)
            if json_match:
                obj = json.loads(json_match.group())
                conditions = obj.get("conditions", [])
                conf = min(max(int(obj.get("confidence", 0)), 0), 100)
                if isinstance(conditions, list):
                    return conditions, conf
        except Exception as e:
            logger.warning("extract_conditions auto/auto failed: %s", e)

    if settings.llm_model == "auto":
        return [], 0

    models_to_try = [settings.llm_model] + _FALLBACK_MODELS
    for model in models_to_try:
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=settings.openai_timeout, verify=False) as client:
                    payload = _build_payload(
                        model=model,
                        messages=[{"role": "user", "content": prompt}],
                        temperature=0.1,
                        max_tokens=1024,
                    )

                    resp = await client.post(
                        f"{settings.openai_base_url}/chat/completions",
                        json=payload,
                        headers=_build_headers(),
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    content = data["choices"][0]["message"]["content"]

                    json_match = re.search(r"\{.*\}", content, re.DOTALL)
                    if json_match:
                        obj = json.loads(json_match.group())
                        conditions = obj.get("conditions", [])
                        conf = min(max(int(obj.get("confidence", 0)), 0), 100)
                        if isinstance(conditions, list):
                            return conditions, conf
            except Exception as e:
                logger.warning("extract_conditions %s attempt %d: %s", model, attempt + 1, e)
                await asyncio.sleep(0.5)

    return [], 0


COVE_EXTRACT_PROMPT = """Ты — юрист-аналитик. Из данного юридического заключения выдели все фактические утверждения, которые можно проверить по исходным статьям законов.

Каждое утверждение переформулируй как вопрос с ответом ДА/НЕТ.

Верни ТОЛЬКО JSON:
{
  "claims": [
    {
      "question": "Вопрос, который можно проверить по тексту статей",
      "expected": "ДА или НЕТ — какой ответ ожидается в заключении"
    }
  ],
  "confidence": 0-100
}

Пример:
Заключение: «Статья 10 ЗоЗПП дает право на возврат товара в течение 14 дней»
→ {"question": "Верно ли, что статья 10 ЗоЗПП дает право на возврат товара?", "expected": "ДА"}"""


COVE_VERIFY_PROMPT = """Ты — строгий эксперт по законам РК. Ответь на вопрос, используя ТОЛЬКО текст предоставленных статей.

Верни ТОЛЬКО JSON:
{
  "answer": "ДА или НЕТ",
  "evidence": "Цитата из статьи, подтверждающая ответ (или null если нет подтверждения)",
  "confidence": 0-100
}

Правила:
- Если в статье нет информации для ответа — answer: "НЕТ", evidence: null
- Не используй свои знания — только текст статей"""


COVE_REVISE_PROMPT = """Ты — юрист РК. Перепиши своё заключение, исправив все ошибки, найденные при проверке.

Исходное заключение содержало ошибки. Исправленный вариант должен опираться ТОЛЬКО на подтверждённые факты.

Формат:
**Ответ:** ...
**Что делать:** ...
**Нормы:** [закон, статья] — суть
**Контрольный вопрос:** <уточняющий вопрос>"""


LEGAL_SYSTEM_PROMPT = """Ты — юрист РК. Используй ТОЛЬКО законы из списка ниже.

ПРАВИЛА:
1. Не выдумывай законы и статьи
2. Если в списке нет подходящего закона — просто напиши "В предоставленных материалах нет статей по вашему вопросу"
3. В разделе "Нормы" указывай ТОЛЬКО название закона и номер статьи из списка
4. Не пиши "Закон РК "О ...", если его нет в списке
5. Не пересчитывай МРП (месячный расчётный показатель) в тенге. Указывай суммы и цифры ТОЛЬКО в той форме, в которой они указаны в тексте статьи. Копируй числа дословно.
6. Не добавляй никаких цифр, сумм и данных, которых нет в тексте предоставленных статей

Верни ТОЛЬКО JSON:
{
  "analysis": "**Ответ:** ...\n**Что делать:** ...\n**Нормы:** [закон, статья] — суть\n**Контрольный вопрос:** ...",
  "confidence": 0-100,
  "control_question": "короткий уточняющий вопрос"
}

Правила для JSON:
- analysis содержит полный юридический анализ
- confidence = насколько ты уверен в ответе
- control_question = 1 короткий вопрос пользователю"""


SELECTION_SYSTEM_PROMPT = """Ты — юрист-эксперт Республики Казахстан.
Твоя задача — по бытовому описанию ситуации определить, какой закон РК и какая статья наиболее применимы.

Верни ТОЛЬКО JSON без пояснений:
{
  "law_title": "Точное название закона из списка",
  "article_number": "Номер статьи",
  "confidence": 0-100,
  "reasoning": "Краткое обоснование (1-2 предложения)"
}

Правила:
- Выбирай ТОЛЬКО из предоставленного списка законов
- confidence = насколько ты уверен, что эта статья подходит (0-100)
- Если ни один закон не подходит — law_title: null, article_number: null, confidence: 0
- Не выдумывай законы и статьи"""


CONTROL_QUESTION_PROMPT = """Ты — юрист РК. На основе ситуации пользователя и выбранного закона/статьи задай 1 уточняющий вопрос.

Вопрос должен помочь подтвердить, что эта статья действительно применима к ситуации.
Верни ТОЛЬКО JSON:
{
  "control_question": "твой уточняющий вопрос пользователю",
  "confidence": 0-100
}

Правила:
- confidence = насколько ты уверен, что вопрос релевантен
- Вопрос должен быть конкретным и по делу
- Не добавляй лишнего текста, только JSON"""


async def generate_legal_analysis(situation: str, pairs: list) -> tuple[str | None, int]:
    if not pairs:
        return (
            "По вашему запросу не найдено конкретных законов. "
            "Рекомендуется проконсультироваться с юристом или "
            "уточнить поисковый запрос."
        ), 0

    articles_text = _format_articles_for_prompt(pairs)
    all_law_names = "\n".join(f"- {law.title} (№{law.number})" if law.number else f"- {law.title}" for law, _ in pairs)
    user_prompt = f"""Ситуация пользователя: {situation}

Доступные законы в базе (используй ТОЛЬКО их):
{all_law_names}

Релевантные статьи из этих законов:
{articles_text}"""

    if settings.ai_provider == "openai":
        return await _call_openai(situation, user_prompt, pairs)
    else:
        result = await _call_ollama(situation, user_prompt)
        if result:
            return result, 50
        return _fallback_analysis(situation, pairs), 30


def _extract_control_question(analysis: str) -> str | None:
    m = re.search(r'\*\*Контрольный вопрос\*\*:\s*(.+?)(?:\n|$)', analysis, re.IGNORECASE)
    if m:
        return m.group(1).strip().lstrip('*').strip()
    m = re.search(r'(?:Контрольный вопрос|Уточняющий вопрос|Вопрос)[:\s]\s*(.+?)(?:\n|$)', analysis, re.IGNORECASE)
    if m:
        return m.group(1).strip().lstrip('*').strip()
    return None


async def _cove_extract_claims(analysis: str) -> tuple[list[dict], int]:
    prompt = f"""Юридическое заключение:
{analysis}"""

    async with httpx.AsyncClient(timeout=settings.openai_timeout, verify=False) as client:
        payload = _build_payload(
            messages=[
                {"role": "system", "content": COVE_EXTRACT_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            max_tokens=512,
        )
        try:
            resp = await client.post(
                f"{settings.openai_base_url}/chat/completions",
                json=payload,
                headers=_build_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            json_match = re.search(r"\{.*\}", content, re.DOTALL)
            if json_match:
                obj = json.loads(json_match.group())
                claims = obj.get("claims", [])
                conf = min(max(int(obj.get("confidence", 0)), 0), 100)
                if claims:
                    return claims, conf
        except Exception as e:
            logger.warning("CoVe extract claims auto/auto failed: %s", e)

    if settings.llm_model == "auto":
        return [], 0

    models_to_try = [settings.llm_model] + _FALLBACK_MODELS
    for model in models_to_try:
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=settings.openai_timeout, verify=False) as client:
                    payload = _build_payload(
                        model=model,
                        messages=[
                            {"role": "system", "content": COVE_EXTRACT_PROMPT},
                            {"role": "user", "content": prompt},
                        ],
                        temperature=0.0,
                        max_tokens=512,
                    )
                    resp = await client.post(
                        f"{settings.openai_base_url}/chat/completions",
                        json=payload,
                        headers=_build_headers(),
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    content = data["choices"][0]["message"]["content"]
                    json_match = re.search(r"\{.*\}", content, re.DOTALL)
                    if json_match:
                        obj = json.loads(json_match.group())
                        claims = obj.get("claims", [])
                        conf = min(max(int(obj.get("confidence", 0)), 0), 100)
                        if claims:
                            return claims, conf
            except Exception as e:
                logger.warning("CoVe extract claims %s attempt %d: %s", model, attempt + 1, e)
                await asyncio.sleep(0.5)
    return [], 0


async def _cove_verify_one(question: str, articles_text: str) -> dict:
    prompt = f"""Статьи законов для проверки:
{articles_text}

Вопрос: {question}"""

    async with httpx.AsyncClient(timeout=settings.openai_timeout, verify=False) as client:
        payload = _build_payload(
            messages=[
                {"role": "system", "content": COVE_VERIFY_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            max_tokens=256,
        )
        try:
            resp = await client.post(
                f"{settings.openai_base_url}/chat/completions",
                json=payload,
                headers=_build_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            json_match = re.search(r"\{.*\}", content, re.DOTALL)
            if json_match:
                obj = json.loads(json_match.group())
                return {
                    "answer": obj.get("answer", "НЕТ"),
                    "evidence": obj.get("evidence"),
                    "confidence": int(obj.get("confidence", 0)),
                }
        except Exception as e:
            logger.warning("CoVe verify auto/auto failed: %s", e)

    if settings.llm_model == "auto":
        return {"answer": "НЕТ", "evidence": None, "confidence": 0}

    models_to_try = [settings.llm_model] + _FALLBACK_MODELS
    for model in models_to_try:
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=settings.openai_timeout, verify=False) as client:
                    payload = _build_payload(
                        model=model,
                        messages=[
                            {"role": "system", "content": COVE_VERIFY_PROMPT},
                            {"role": "user", "content": prompt},
                        ],
                        temperature=0.0,
                        max_tokens=256,
                    )
                    resp = await client.post(
                        f"{settings.openai_base_url}/chat/completions",
                        json=payload,
                        headers=_build_headers(),
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    content = data["choices"][0]["message"]["content"]
                    json_match = re.search(r"\{.*\}", content, re.DOTALL)
                    if json_match:
                        obj = json.loads(json_match.group())
                        return {
                            "answer": obj.get("answer", "НЕТ"),
                            "evidence": obj.get("evidence"),
                            "confidence": int(obj.get("confidence", 0)),
                        }
            except Exception as e:
                logger.warning("CoVe verify %s attempt %d: %s", model, attempt + 1, e)
                await asyncio.sleep(0.5)
    return {"answer": "НЕТ", "evidence": None, "confidence": 0}


async def _cove_revise(situation: str, analysis: str, verification_log: list[dict], pairs: list) -> tuple[str, int]:
    log_text = "\n".join(
        f"Утверждение: {v.get('question', '')}\nПроверка: {v.get('answer', 'НЕТ')}\nДоказательство: {v.get('evidence', 'нет')}\n"
        for v in verification_log
    )
    failed = sum(1 for v in verification_log if v.get("answer") == "НЕТ")
    confidence = max(0, 100 - (failed * 100 // max(len(verification_log), 1)))

    if failed == 0:
        return analysis, 100

    articles_text = _format_articles_for_prompt(pairs) if pairs else "Нет статей"
    prompt = f"""Ситуация пользователя: {situation}

Исходное заключение (содержит ошибки):
{analysis}

Результаты проверки фактов:
{log_text}

Доступные статьи:
{articles_text}

Исправь заключение: убери ошибочные утверждения, оставь только подтверждённые факты."""

    async with httpx.AsyncClient(timeout=settings.openai_timeout, verify=False) as client:
        payload = _build_payload(
            messages=[
                {"role": "system", "content": COVE_REVISE_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            max_tokens=1536,
        )
        try:
            resp = await client.post(
                f"{settings.openai_base_url}/chat/completions",
                json=payload,
                headers=_build_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            revised = data["choices"][0]["message"]["content"].strip()
            if revised and len(revised) > 50:
                return revised, confidence
        except Exception as e:
            logger.warning("CoVe revise auto/auto failed: %s", e)

    if settings.llm_model == "auto":
        return analysis, confidence

    models_to_try = [settings.llm_model] + _FALLBACK_MODELS
    for model in models_to_try:
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=settings.openai_timeout, verify=False) as client:
                    payload = _build_payload(
                        model=model,
                        messages=[
                            {"role": "system", "content": COVE_REVISE_PROMPT},
                            {"role": "user", "content": prompt},
                        ],
                        temperature=0.2,
                        max_tokens=1536,
                    )
                    resp = await client.post(
                        f"{settings.openai_base_url}/chat/completions",
                        json=payload,
                        headers=_build_headers(),
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    revised = data["choices"][0]["message"]["content"].strip()
                    if revised and len(revised) > 50:
                        return revised, confidence
            except Exception as e:
                logger.warning("CoVe revise %s attempt %d: %s", model, attempt + 1, e)
                await asyncio.sleep(0.5)
    return analysis, confidence


async def cove_verify(
    situation: str,
    analysis: str,
    pairs: list,
) -> dict:
    if not analysis or not pairs:
        return {"analysis": analysis, "confidence": 0, "errors": []}

    articles_text = _format_articles_for_prompt(pairs)

    claims, claims_conf = await _cove_extract_claims(analysis)
    if not claims:
        logger.warning("CoVe: no claims extracted from analysis")
        return {"analysis": analysis, "confidence": 50, "errors": []}

    verification_log = []
    for claim in claims:
        result = await _cove_verify_one(claim["question"], articles_text)
        verification_log.append({
            "question": claim["question"],
            "expected": claim.get("expected", "ДА"),
            "answer": result["answer"],
            "evidence": result["evidence"],
            "confidence": result["confidence"],
        })

    errors = [v for v in verification_log if v["answer"] != v["expected"]]
    revised_analysis, revised_confidence = await _cove_revise(situation, analysis, verification_log, pairs)

    return {
        "analysis": revised_analysis,
        "confidence": revised_confidence,
        "errors": [{"claim": e["question"], "expected": e["expected"], "actual": e["answer"]} for e in errors],
    }


async def _call_ollama(situation: str, user_prompt: str) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=settings.ollama_timeout) as client:
            payload = {
                "model": settings.ollama_model,
                "system": LEGAL_SYSTEM_PROMPT,
                "prompt": user_prompt,
                "stream": False,
                "options": {"temperature": 0.3, "num_predict": 2048},
            }
            resp = await client.post(
                f"{settings.ollama_host}/api/generate",
                json=payload,
            )
            resp.raise_for_status()
            result = resp.json()
            analysis = result.get("response", "").strip()
            if analysis:
                return analysis
    except httpx.ConnectError:
        logger.warning("Ollama недоступен (%s) — используется шаблонный анализ", settings.ollama_host)
    except httpx.TimeoutException:
        logger.warning("Ollama timeout (%sс) — используется шаблонный анализ", settings.ollama_timeout)
    except Exception as e:
        logger.exception("Ошибка Ollama (%s)", e)
    return None


def _is_unhelpful(text: str, pairs: list | None = None) -> bool:
    if not text or len(text) < 20:
        return True
    if pairs:
        phrases = [
            "не найдено", "нет статей", "нет конкретных", "нет информации",
            "не могу предоставить", "недостаточно данных",
        ]
        if any(p in text.lower() for p in phrases):
            return True
    return False


_FALLBACK_MODELS = ["llama-3.1-8b-instant", "llama-3.3-70b-versatile"]


async def _try_openai_model(model: str = "auto", user_prompt: str = "", pairs: list | None = None) -> tuple[str | None, int]:
    try:
        full_user = f"{LEGAL_SYSTEM_PROMPT}\n\n{user_prompt}"
        async with httpx.AsyncClient(timeout=settings.openai_timeout, verify=False) as client:
            payload = _build_payload(
                model=model,
                messages=[{"role": "user", "content": full_user}],
                temperature=0.3,
                max_tokens=2048,
            )

            resp = await client.post(
                f"{settings.openai_base_url}/chat/completions",
                json=payload,
                headers=_build_headers(),
            )
            resp.raise_for_status()
            result = resp.json()
            msg = result["choices"][0]["message"]
            content = (msg.get("content") or "").strip()
            if not content:
                return None, 0

            json_match = re.search(r"\{.*\}", content, re.DOTALL)
            if json_match:
                try:
                    obj = json.loads(json_match.group())
                    analysis = obj.get("analysis", "").strip()
                    conf = min(max(int(obj.get("confidence", 0)), 0), 100)
                    if analysis and not _is_unhelpful(analysis, pairs):
                        return analysis, conf
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass

            if content and not _is_unhelpful(content, pairs):
                return content, 50
    except Exception as e:
        logger.warning("Model %s failed (%s)", model, e)

    return None, 0


async def _call_openai(situation: str, user_prompt: str, pairs: list | None = None) -> tuple[str | None, int]:
    if settings.llm_model == "auto":
        result, conf = await _try_openai_model(user_prompt=user_prompt, pairs=pairs)
        if result:
            return result, conf
        return _fallback_analysis(situation, pairs), 30

    best_result = None
    best_conf = 0

    result, conf = await _try_openai_model(user_prompt=user_prompt, pairs=pairs)
    if result and conf >= best_conf:
        best_result, best_conf = result, conf

    models_to_try = [settings.llm_model] + _FALLBACK_MODELS
    for model in models_to_try:
        result, conf = await _try_openai_model(model, user_prompt, pairs)
        if result and conf > best_conf:
            best_result, best_conf = result, conf

    discovered = await _discover_models()
    already_tried = set(models_to_try)
    for model in discovered:
        if model in already_tried:
            continue
        already_tried.add(model)
        result, conf = await _try_openai_model(model, user_prompt, pairs)
        if result and conf > best_conf:
            best_result, best_conf = result, conf
            logger.warning("LLM analysis succeeded via discovered model: %s", model)

    if best_result:
        return best_result, best_conf
    return _fallback_analysis(situation, pairs), 30


def _format_articles_for_prompt(pairs: list) -> str:
    lines = []
    for law, articles in pairs[:15]:
        cat_name = law.category.name if law.category else ""
        law_ref = law.title
        if law.number:
            law_ref += f" (№{law.number})"
        lines.append(f"Закон: {law_ref}")
        if cat_name:
            lines.append(f"  Категория: {cat_name}")
        for art in articles[:3]:
            art_ref = f"  Статья {art.number or '?'}"
            if art.title:
                art_ref += f": {art.title}"
            lines.append(art_ref)
            if art.content:
                content = art.content[:500].replace("\n", " ").strip()
                lines.append(f"    Текст: «{content}…»")
        lines.append("")
    if len(pairs) > 15:
        lines.append(f"... и ещё {len(pairs) - 15} закона(ов) в базе")
    return "\n".join(lines)


def _fallback_analysis(situation: str, pairs: list) -> str:
    import re
    q_lower = situation.lower()
    q_tokens = [t for t in re.findall(r'[а-яёa-z]+', q_lower) if len(t) > 3]

    parts = [f"**Ситуация:** {situation}", ""]
    matched_any = False

    for law, articles in pairs:
        law_ref = law.title
        if law.number:
            law_ref += f" (№{law.number})"
        relevant = []
        for art in articles:
            content = (art.content or "").lower()
            title = (art.title or "").lower()
            matched_tokens = [t for t in q_tokens if t in content or t in title]
            if matched_tokens:
                relevant.append((art, matched_tokens))
        if not relevant:
            continue
        matched_any = True
        parts.append(f"**{law_ref}**")
        for art, tokens in relevant[:3]:
            excerpt = (art.content or "")[:300].replace("\n", " ").strip()
            ref = f"Статья {art.number or '?'}"
            if art.title:
                ref += f": {art.title}"
            parts.append(f"  • {ref}")
            if excerpt:
                parts.append(f"    {excerpt}…")

    if not matched_any:
        parts.append("По вашему вопросу не найдено статей с прямым ответом.")
        parts.append("Рекомендуется обратиться к юристу для детальной консультации.")
    else:
        parts.append("")
        parts.append("**Рекомендация:**")
        parts.append("Для получения официального разъяснения обратитесь к квалифицированному юристу.")

    parts.append("")
    parts.append("⚠ Данная информация носит справочный характер.")
    return "\n".join(parts)
