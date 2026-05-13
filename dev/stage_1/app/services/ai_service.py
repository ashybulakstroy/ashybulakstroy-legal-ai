import asyncio
import json
import logging
import re

import httpx
from sqlalchemy import select

from app.config import settings
from app.models.legislation import QueryExpansionCache

logger = logging.getLogger(__name__)

_expansion_cache: dict[str, str] = {}
_MAX_RETRIES = 10

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
            headers = {}
            if settings.openai_api_key and settings.openai_api_key != "not-needed":
                headers["Authorization"] = f"Bearer {settings.openai_api_key}"

            async with httpx.AsyncClient(timeout=10, verify=False) as client:
                resp = await client.get(
                    f"{settings.openai_base_url}/models",
                    headers=headers,
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


def _parse_expansion(raw: str) -> str | None:
    text = raw.strip()
    if not text:
        return None

    json_match = re.search(r"\{.*\}", text, re.DOTALL)
    if json_match:
        try:
            obj = json.loads(json_match.group())
            q = obj.get("search_query", "")
            if isinstance(q, str) and q.strip():
                words = q.strip().split()
                if 1 <= len(words) <= 10:
                    return " ".join(words[:10])
        except (json.JSONDecodeError, TypeError):
            pass

    words = re.findall(r"[а-яёa-z]+", text.lower())
    words = [w for w in words if len(w) > 2]
    if 1 <= len(words) <= 10:
        return " ".join(words[:10])

    return None


async def _try_expansion_model(query: str, model: str, use_provider_auto: bool) -> str | None:
    headers = {"Content-Type": "application/json"}
    if settings.openai_api_key and settings.openai_api_key != "not-needed":
        headers["Authorization"] = f"Bearer {settings.openai_api_key}"

    prompt = (
        "Преобразуй бытовой запрос в поисковые юридические термины.\n"
        "Верни ТОЛЬКО JSON: {\"search_query\": \"термин1 термин2 термин3\"}\n"
        "2-5 ключевых слов, без пояснений.\n\n"
        f"Запрос: {query}"
    )

    try:
        async with httpx.AsyncClient(timeout=settings.openai_timeout, verify=False) as client:
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 512,
            }
            if use_provider_auto:
                payload["provider"] = "auto"

            resp = await client.post(
                f"{settings.openai_base_url}/chat/completions",
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
            msg = data["choices"][0]["message"]

            content = (msg.get("content") or "").strip()
            parsed = _parse_expansion(content)
            if parsed:
                return parsed

            reasoning = (msg.get("reasoning") or "").strip()
            parsed = _parse_expansion(reasoning)
            if parsed:
                return parsed

            logger.warning("Expansion model %s (provider=auto=%s) no parseable output", model, use_provider_auto)
    except Exception as e:
        logger.warning("Expansion model %s (provider=auto=%s) failed (%s)", model, use_provider_auto, e)

    return None


async def _call_expansion(query: str) -> str | None:
    models_to_try = [settings.openai_model] + _FALLBACK_MODELS

    for model in models_to_try:
        result = await _try_expansion_model(query, model, False)
        if result is not None:
            return result

    discovered_triggered = False
    for model in models_to_try:
        result = await _try_expansion_model(query, model, True)
        if result is not None:
            return result
        if not discovered_triggered:
            discovered_triggered = True

    if discovered_triggered:
        discovered = await _discover_models()
        already_tried = set(models_to_try)
        for model in discovered:
            if model in already_tried:
                continue
            already_tried.add(model)
            result = await _try_expansion_model(query, model, False)
            if result is not None:
                logger.warning("Query expansion succeeded via discovered model: %s", model)
                return result

    return None


async def expand_query_for_search(query: str, db) -> str | None:
    cached = _expansion_cache.get(query)
    if cached:
        logger.info("Query expansion cache HIT (memory): %r -> %r", query, cached)
        return cached

    result = await db.execute(
        select(QueryExpansionCache).where(QueryExpansionCache.original_query == query)
    )
    row = result.scalar_one_or_none()
    if row:
        _expansion_cache[query] = row.expanded_query
        logger.info("Query expansion cache HIT (db): %r -> %r", query, row.expanded_query)
        return row.expanded_query

    last_error = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            expanded = await _call_expansion(query)
            if expanded:
                _expansion_cache[query] = expanded
                db.add(QueryExpansionCache(original_query=query, expanded_query=expanded))
                await db.commit()
                logger.info("Query expanded (attempt %d): %r -> %r", attempt, query, expanded)
                return expanded

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
    return None

LEGAL_SYSTEM_PROMPT = """Ты — юрист РК. Используй ТОЛЬКО законы из списка ниже.

ПРАВИЛА:
1. Не выдумывай законы и статьи
2. Если в списке нет подходящего закона — просто напиши "В предоставленных материалах нет статей по вашему вопросу"
3. В разделе "Нормы" указывай ТОЛЬКО название закона и номер статьи из списка
4. Не пиши "Закон РК "О ...", если его нет в списке
5. Не пересчитывай МРП (месячный расчётный показатель) в тенге. Указывай суммы и цифры ТОЛЬКО в той форме, в которой они указаны в тексте статьи. Копируй числа дословно.
6. Не добавляй никаких цифр, сумм и данных, которых нет в тексте предоставленных статей

Формат:
**Ответ:** ...
**Что делать:** ...
**Нормы:** [название закона из списка, статья] — суть"""


async def generate_legal_analysis(situation: str, pairs: list) -> str:
    if not pairs:
        return (
            "По вашему запросу не найдено конкретных законов. "
            "Рекомендуется проконсультироваться с юристом или "
            "уточнить поисковый запрос."
        )

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
        return await _call_ollama(situation, user_prompt) or _fallback_analysis(situation, pairs)


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


async def _try_openai_model(model: str, user_prompt: str, pairs: list | None, use_provider_auto: bool) -> str | None:
    try:
        headers = {"Content-Type": "application/json"}
        if settings.openai_api_key and settings.openai_api_key != "not-needed":
            headers["Authorization"] = f"Bearer {settings.openai_api_key}"

        full_user = f"{LEGAL_SYSTEM_PROMPT}\n\n{user_prompt}"
        async with httpx.AsyncClient(timeout=settings.openai_timeout, verify=False) as client:
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": full_user}],
                "temperature": 0.3,
                "max_tokens": 2048,
            }
            if use_provider_auto:
                payload["provider"] = "auto"

            resp = await client.post(
                f"{settings.openai_base_url}/chat/completions",
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            result = resp.json()
            msg = result["choices"][0]["message"]
            analysis = (msg.get("content") or msg.get("reasoning") or "").strip()
            if analysis and not _is_unhelpful(analysis, pairs):
                return analysis
            logger.warning("Model %s (provider=auto=%s) returned unhelpful response%s",
                          model, use_provider_auto, f" (pairs={len(pairs)})" if pairs else "")
    except Exception as e:
        logger.warning("Model %s (provider=auto=%s) failed (%s)", model, use_provider_auto, e)

    return None


async def _call_openai(situation: str, user_prompt: str, pairs: list | None = None) -> str:
    models_to_try = [settings.openai_model] + _FALLBACK_MODELS

    for model in models_to_try:
        result = await _try_openai_model(model, user_prompt, pairs, False)
        if result is not None:
            return result

    had_404 = False
    for model in models_to_try:
        result = await _try_openai_model(model, user_prompt, pairs, True)
        if result is not None:
            return result

    discovered = await _discover_models()
    already_tried = set(models_to_try)
    for model in discovered:
        if model in already_tried:
            continue
        already_tried.add(model)
        result = await _try_openai_model(model, user_prompt, pairs, False)
        if result is not None:
            logger.warning("LLM analysis succeeded via discovered model: %s", model)
            return result

    return _fallback_analysis(situation, pairs)


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
