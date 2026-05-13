import asyncio
import json
import logging
import re
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import get_db
from app.models.legislation import Law, Article, LawStatus, SearchCache
from app.schemas.legislation import (
    CategoryResponse, CategoryTree, LawResponse, SearchResult,
    LegalAdviceRequest, LegalAdviceResponse,
)
from app.services.legislation_service import LegislationService
from app.services.ai_service import (
    generate_legal_analysis, expand_query_for_search,
    select_law_article_llm, _extract_control_question,
    cove_verify,
)

router = APIRouter(prefix="/api/v1", tags=["Законодательство РК"])


async def get_service(db: AsyncSession = Depends(get_db)) -> LegislationService:
    return LegislationService(db)


async def _load_cached_pairs(search_text: str, db: AsyncSession, service: LegislationService) -> list | None:
    try:
        row = await db.execute(
            select(SearchCache).where(SearchCache.search_text == search_text)
        )
        row = row.scalar_one_or_none()
        if not row:
            return None
        if row.cache_version != settings.cache_version:
            await db.delete(row)
            await db.commit()
            return None
        if datetime.utcnow() - row.created_at > timedelta(days=settings.cache_ttl_days):
            await db.delete(row)
            await db.commit()
            return None

        data = json.loads(row.result_json)
        pairs = []
        for law_id, article_ids in data:
            law = await db.execute(
                select(Law).options(selectinload(Law.category), selectinload(Law.articles)).where(Law.id == law_id)
            )
            law = law.scalar_one_or_none()
            if not law:
                continue
            art_map = {a.id: a for a in (law.articles or [])}
            articles = [art_map[a_id] for a_id in article_ids if a_id in art_map]
            if articles:
                pairs.append((law, articles))
        return pairs or None
    except Exception:
        return None


async def _save_cached_pairs(search_text: str, pairs: list, db: AsyncSession):
    try:
        data = json.dumps([[law.id, [a.id for a in arts]] for law, arts in pairs])
        existing = await db.execute(
            select(SearchCache).where(SearchCache.search_text == search_text)
        )
        row = existing.scalar_one_or_none()
        if row:
            row.result_json = data
            row.cache_version = settings.cache_version
            row.created_at = datetime.utcnow()
        else:
            db.add(SearchCache(search_text=search_text, result_json=data, cache_version=settings.cache_version))
        await db.commit()
    except Exception:
        await db.rollback()


@router.get("/categories", response_model=list[CategoryTree])
async def get_categories(
    service: LegislationService = Depends(get_service),
):
    cats = await service.get_category_tree()
    return [_category_to_tree(c) for c in cats]


@router.get("/categories/{slug}", response_model=CategoryResponse)
async def get_category(
    slug: str,
    service: LegislationService = Depends(get_service),
):
    cat = await service.get_category_by_slug(slug)
    if not cat:
        raise HTTPException(status_code=404, detail="Категория не найдена")
    return cat


@router.get("/laws/{law_id}", response_model=LawResponse)
async def get_law(
    law_id: int,
    service: LegislationService = Depends(get_service),
):
    law = await service.get_law(law_id)
    if not law:
        raise HTTPException(status_code=404, detail="Закон не найден")
    return law


@router.get("/search", response_model=list[SearchResult])
async def search(
    q: str = Query(..., min_length=2),
    limit: int = Query(20, le=100),
    service: LegislationService = Depends(get_service),
):
    import re
    from urllib.parse import unquote
    laws = await service.search_laws(q, limit)
    q_clean = q.lower().strip()
    q_tokens = [t for t in re.findall(r'[а-яёa-z]+', q_clean) if len(t) > 2]
    results = []
    for law in laws:
        score = _calculate_simple_score(law, q)
        match_count = 0
        for article in (law.articles or []):
            content = (article.content or "").lower()
            title = (article.title or "").lower()
            if q_clean in content or q_clean in title or any(t in content for t in q_tokens):
                match_count += 1
        results.append(SearchResult(
            id=law.id,
            title=law.title,
            number=law.number,
            summary=law.summary,
            status=law.status.value if hasattr(law.status, 'value') else law.status,
            category_name=law.category.name if law.category else "",
            score=score,
            match_count=match_count,
        ))
    results.sort(key=lambda r: r.match_count, reverse=True)
    return results


@router.post("/legal-advice", response_model=LegalAdviceResponse)
async def legal_advice(
    req: LegalAdviceRequest,
    service: LegislationService = Depends(get_service),
):
    from app.schemas.legislation import ArticleExcerpt

    search_text = req.situation
    if req.context:
        search_text = f"{req.situation} {req.context}"

    pairs = None
    expanded_query = None
    llm_selection = None
    confidence = 0

    if req.search_method == "llm":
        laws_result = await service.db.execute(
            select(Law).options(selectinload(Law.category)).where(
                Law.status.in_([LawStatus.ACTIVE, LawStatus.AMENDED])
            )
        )
        all_laws = list(laws_result.scalars().all())
        law_list = [
            (law.id, law.title, law.number, law.category.name if law.category else "")
            for law in all_laws
        ]

        llm_selection = await select_law_article_llm(search_text, law_list)
        confidence = llm_selection.get("confidence", 0)

        if llm_selection.get("law_title") and confidence >= 30:
            selected_law = None
            for law in all_laws:
                if law.title == llm_selection["law_title"]:
                    selected_law = law
                    break
            if selected_law:
                articles_stmt = select(Article).where(
                    Article.law_id == selected_law.id
                ).order_by(Article.sort_order)
                articles_result = await service.db.execute(articles_stmt)
                all_articles = list(articles_result.scalars().all())

                article_num = llm_selection.get("article_number", "")
                selected_articles = []
                if article_num:
                    for art in all_articles:
                        if art.number and art.number.strip() == article_num.strip():
                            selected_articles = [art]
                            break
                if not selected_articles and all_articles:
                    selected_articles = all_articles[:3]

                if selected_articles:
                    pairs = [(selected_law, selected_articles)]

        if not pairs:
            pairs = await service.find_relevant_articles(search_text)

    else:
        if not req.refresh:
            pairs = await _load_cached_pairs(search_text, service.db, service)

        if pairs is None:
            pairs = await service.find_relevant_articles(search_text)

            if not pairs and req.search_method == "auto":
                expanded_query = await expand_query_for_search(search_text, service.db)
                if expanded_query:
                    pairs = await service.find_relevant_articles(expanded_query)

            if pairs:
                await _save_cached_pairs(search_text, pairs, service.db)

    results = []
    article_excerpts = []
    for law, articles in pairs:
        results.append(SearchResult(
            id=law.id,
            title=law.title,
            number=law.number,
            summary=law.summary,
            status=law.status.value if hasattr(law.status, 'value') else law.status,
            category_name=law.category.name if law.category else "",
            score=1.0,
            match_count=len(articles),
        ))
        for art in articles:
            article_excerpts.append(ArticleExcerpt(
                id=art.id,
                number=art.number,
                title=art.title,
                content=art.content,
                law_id=law.id,
                law_title=law.title,
            ))
    control_question = None
    analysis = await generate_legal_analysis(search_text, pairs)
    if analysis:
        control_question = _extract_control_question(analysis)

    verification = await cove_verify(search_text, analysis, pairs)
    if verification.get("analysis"):
        analysis = verification["analysis"]
        control_question = _extract_control_question(analysis) or control_question
    if verification.get("confidence", 0) > 0:
        confidence = verification["confidence"]

    if analysis and confidence > 0:
        analysis = analysis.replace("**Ответ:** ", f"**Ответ:** [{confidence}%] ", 1)

    refinement_hint = None
    if not pairs:
        if expanded_query:
            refinement_hint = (
                "По вашему запросу ничего не найдено даже после расширения. "
                "Попробуйте переформулировать — добавьте больше деталей: "
                "что именно произошло, с кем, где."
            )
        else:
            refinement_hint = (
                "По вашему запросу ничего не найдено. "
                "Попробуйте добавить контекст или использовать юридические термины "
                "(например, «недостаток товара» вместо «сломался»)."
            )

    # Filter to only articles the LLM actually cited in the analysis
    if analysis and article_excerpts:
        cited_nums = set()
        for m in re.finditer(
            r'(?:стать[яиюеях]|ст\.?)\s*(\d+(?:\s*[-,]\s*\d+)*(?:-\d+)?)',
            analysis.lower()
        ):
            raw = m.group(1)
            for part in re.findall(r'\d+(?:-\d+)?', raw):
                cited_nums.add(part)
        if not cited_nums:
            article_excerpts = []
        else:
            filtered = []
            for a in article_excerpts:
                art_num = str(a.number or '').strip()
                if not art_num:
                    continue
                if art_num in cited_nums:
                    filtered.append(a)
                    continue
                base = re.match(r'(\d+)', art_num)
                if base and base.group(1) in cited_nums:
                    filtered.append(a)
            article_excerpts = filtered

    return LegalAdviceResponse(
        situation=req.situation,
        relevant_laws=results,
        relevant_articles=article_excerpts,
        analysis=analysis,
        expanded_query=expanded_query,
        refinement_hint=refinement_hint,
        confidence=confidence,
        control_question=control_question,
    )


def _category_to_tree(cat) -> CategoryTree:
    return CategoryTree(
        id=cat.id,
        name=cat.name,
        slug=cat.slug,
        description=cat.description,
        type=cat.type.value if hasattr(cat.type, 'value') else cat.type,
        parent_id=cat.parent_id,
        sort_order=cat.sort_order,
        laws=[],
        children=[_category_to_tree(c) for c in cat.children] if hasattr(cat, 'children') else [],
    )


def _calculate_simple_score(law, query: str) -> float:
    q = query.lower()
    score = 0.0
    if law.title and q in law.title.lower():
        score += 10.0
    if law.summary and q in law.summary.lower():
        score += 5.0
    if law.full_text and q in law.full_text.lower():
        count = law.full_text.lower().count(q)
        score += min(count * 0.5, 20.0)
    if law.number and q in law.number.lower():
        score += 3.0
    return score


def _generate_analysis(situation: str, pairs: list) -> str:
    if not pairs:
        return (
            "По вашему запросу не найдено конкретных законов. "
            "Рекомендуется проконсультироваться с юристом или "
            "уточнить поисковый запрос."
        )

    lines = []
    total_articles = 0
    for law, articles in pairs:
        total_articles += len(articles)
        cat_name = law.category.name if law.category else ""
        law_ref = law.title
        if law.number:
            law_ref += f" (№{law.number})"
        lines.append(f"📌 {law_ref}")
        if cat_name:
            lines.append(f"   Категория: {cat_name}")
        for art in articles[:3]:
            art_ref = f"   — Статья {art.number or '?'}"
            if art.title:
                art_ref += f": {art.title}"
            lines.append(art_ref)
            if art.content:
                excerpt = art.content[:150].replace('\n', ' ').strip()
                if excerpt:
                    lines.append(f"     «{excerpt}…»")
        if len(articles) > 3:
            lines.append(f"     … и ещё {len(articles) - 3} статей")

    recommendations = []
    if total_articles > 0:
        recommendations.append(
            f"На основе найденных статей ({total_articles} шт.) можно определить "
            f"применимые нормы права. Рекомендуется внимательно изучить указанные статьи."
        )

    parts = [
        "── АНАЛИЗ СИТУАЦИИ ──\n",
        *lines,
        "",
        "── РЕКОМЕНДАЦИИ ──",
        *recommendations,
        "",
        "⚠ Данная информация носит справочный характер. "
        "Для получения официальной консультации обратитесь к квалифицированному юристу.",
    ]
    return "\n".join(parts)


def _sse_progress(percent: int, message: str) -> str:
    return f"data: {json.dumps({'type': 'progress', 'percent': percent, 'message': message}, ensure_ascii=False)}\n\n"


def _sse_event(event_type: str, data: dict) -> str:
    return f"data: {json.dumps({'type': event_type, **data}, ensure_ascii=False)}\n\n"


@router.post("/legal-advice/stream")
async def legal_advice_stream(req: LegalAdviceRequest):
    from app.database import async_session_factory
    from app.schemas.legislation import ArticleExcerpt

    async def event_stream():
        try:
            async with async_session_factory() as db:
                service = LegislationService(db)
                search_text = req.situation
                if req.context:
                    search_text = f"{req.situation} {req.context}"

                pairs = None
                expanded_query = None
                confidence = 0

                if req.search_method == "llm":
                    yield _sse_progress(10, "Выбор закона через нейросеть...")
                    laws_result = await db.execute(
                        select(Law).options(selectinload(Law.category)).where(
                            Law.status.in_([LawStatus.ACTIVE, LawStatus.AMENDED])
                        )
                    )
                    all_laws = list(laws_result.scalars().all())
                    law_list = [
                        (law.id, law.title, law.number, law.category.name if law.category else "")
                        for law in all_laws
                    ]
                    llm_selection = await select_law_article_llm(search_text, law_list)
                    confidence = llm_selection.get("confidence", 0)

                    if llm_selection.get("law_title") and confidence >= 30:
                        selected_law = next((law for law in all_laws if law.title == llm_selection["law_title"]), None)
                        if selected_law:
                            articles_result = await db.execute(
                                select(Article).where(Article.law_id == selected_law.id).order_by(Article.sort_order)
                            )
                            all_articles = list(articles_result.scalars().all())
                            article_num = llm_selection.get("article_number", "")
                            selected_articles = []
                            if article_num:
                                selected_articles = [a for a in all_articles if a.number and a.number.strip() == article_num.strip()]
                            if not selected_articles and all_articles:
                                selected_articles = all_articles[:3]
                            if selected_articles:
                                pairs = [(selected_law, selected_articles)]
                                yield _sse_progress(30, f"Выбран закон: {selected_law.title}")

                    if not pairs:
                        yield _sse_progress(15, "Поиск релевантных статей в БД...")
                        pairs = await service.find_relevant_articles(search_text)
                else:
                    yield _sse_progress(10, "Поиск релевантных статей в БД...")
                    if not req.refresh:
                        pairs = await _load_cached_pairs(search_text, db, service)
                    if pairs is None:
                        pairs = await service.find_relevant_articles(search_text)
                        if not pairs and req.search_method == "auto":
                            yield _sse_progress(20, "Расширение запроса через нейросеть...")
                            expanded_query = await expand_query_for_search(search_text, db)
                            if expanded_query:
                                yield _sse_progress(25, "Повторный поиск...")
                                pairs = await service.find_relevant_articles(expanded_query)
                        if pairs:
                            await _save_cached_pairs(search_text, pairs, db)

                yield _sse_progress(40, "Формирование списка законов и статей...")
                results = []
                article_excerpts = []
                for law, articles in pairs:
                    results.append(SearchResult(
                        id=law.id, title=law.title, number=law.number,
                        summary=law.summary,
                        status=law.status.value if hasattr(law.status, 'value') else law.status,
                        category_name=law.category.name if law.category else "",
                        score=1.0, match_count=len(articles),
                    ))
                    for art in articles:
                        article_excerpts.append(ArticleExcerpt(
                            id=art.id, number=art.number, title=art.title,
                            content=art.content, law_id=law.id, law_title=law.title,
                        ))

                yield _sse_progress(50, "Анализ ситуации через нейросеть...")
                control_question = None
                analysis = await generate_legal_analysis(search_text, pairs)
                if analysis:
                    control_question = _extract_control_question(analysis)

                yield _sse_progress(75, "Проверка фактов (самопроверка)...")
                verification = await cove_verify(search_text, analysis, pairs)
                if verification.get("analysis"):
                    analysis = verification["analysis"]
                    control_question = _extract_control_question(analysis) or control_question
                if verification.get("confidence", 0) > 0:
                    confidence = verification["confidence"]

                yield _sse_progress(90, "Фильтрация цитированных статей...")
                refinement_hint = None
                if not pairs:
                    if expanded_query:
                        refinement_hint = (
                            "По вашему запросу ничего не найдено даже после расширения. "
                            "Попробуйте переформулировать."
                        )
                    else:
                        refinement_hint = (
                            "По вашему запросу ничего не найдено. "
                            "Попробуйте добавить контекст."
                        )

                if analysis and article_excerpts:
                    cited_nums = set()
                    for m in re.finditer(
                        r'(?:стать[яиюеях]|ст\.?)\s*(\d+(?:\s*[-,]\s*\d+)*(?:-\d+)?)',
                        analysis.lower()
                    ):
                        raw = m.group(1)
                        for part in re.findall(r'\d+(?:-\d+)?', raw):
                            cited_nums.add(part)
                    if cited_nums:
                        filtered = []
                        for a in article_excerpts:
                            art_num = str(a.number or '').strip()
                            if not art_num:
                                continue
                            if art_num in cited_nums:
                                filtered.append(a)
                                continue
                            base = re.match(r'(\d+)', art_num)
                            if base and base.group(1) in cited_nums:
                                filtered.append(a)
                        article_excerpts = filtered
                    else:
                        article_excerpts = []

                yield _sse_progress(100, "Готово")

                result = LegalAdviceResponse(
                    situation=req.situation,
                    relevant_laws=results,
                    relevant_articles=article_excerpts,
                    analysis=analysis,
                    expanded_query=expanded_query,
                    refinement_hint=refinement_hint,
                    confidence=confidence,
                    control_question=control_question,
                )

                yield _sse_event("result", result.model_dump(mode="json"))

        except Exception as e:
            logger.exception("Error in streaming legal advice")
            yield _sse_progress(0, "Ошибка при обработке запроса")
            yield _sse_event("error", {"detail": str(e)})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
