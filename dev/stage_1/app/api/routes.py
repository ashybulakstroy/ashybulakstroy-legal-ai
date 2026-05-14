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
from app.models.legislation import Law, Article, LawStatus, SearchCache, ArticleCondition, LegalAdviceLog
from app.schemas.legislation import (
    CategoryResponse, CategoryTree, LawResponse, SearchResult,
    LegalAdviceRequest, LegalAdviceResponse,
)
from app.services.legislation_service import LegislationService
from app.services.ai_service import (
    generate_legal_analysis, expand_query_for_search,
    select_law_article_llm, _extract_control_question,
    cove_verify, check_completeness, extract_conditions_from_article,
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


MAX_CONDITION_ARTICLES = 5


async def _get_or_create_conditions(article: Article, db: AsyncSession) -> list[ArticleCondition]:
    result = await db.execute(
        select(ArticleCondition).where(ArticleCondition.article_id == article.id)
    )
    conditions = list(result.scalars().all())
    if conditions:
        return conditions

    conditions_data = await extract_conditions_from_article(article.title or "", article.content or "")
    if not conditions_data:
        return []

    created = []
    for cd in conditions_data:
        cond = ArticleCondition(
            article_id=article.id,
            condition_text=cd.get("condition_text", ""),
            condition_type=cd.get("condition_type", "other"),
            is_required=cd.get("is_required", True),
        )
        db.add(cond)
        created.append(cond)
    await db.commit()
    return created


def _format_articles_with_conditions(pairs: list, condition_map: dict[int, list[ArticleCondition]]) -> str:
    lines = []
    for law, articles in pairs[:3]:
        law_ref = law.title
        if law.number:
            law_ref += f" (№{law.number})"
        lines.append(f"Закон: {law_ref}")
        for art in articles[:5]:
            art_ref = f"  Статья {art.number or '?'}"
            if art.title:
                art_ref += f": {art.title}"
            lines.append(art_ref)
            conditions = condition_map.get(art.id, [])
            for cond in conditions:
                req = "(обязательное)" if cond.is_required else "(необязательное)"
                lines.append(f"    - {cond.condition_text} {req}")
            if art.content:
                content_preview = art.content[:200].replace("\n", " ").strip()
                lines.append(f"    Текст: «{content_preview}…»")
        lines.append("")
    return "\n".join(lines)


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

    _ml = json.dumps([{"id": r.id, "title": r.title, "number": r.number, "category": r.category_name} for r in results], ensure_ascii=False)
    _mc = json.dumps(list({r.category_name for r in results if r.category_name}), ensure_ascii=False)
    service.db.add(LegalAdviceLog(
        situation=q, client_type="forms", search_method="auto", endpoint="search",
        status="PASS", confidence=0, completeness=100,
        matched_laws=_ml, matched_categories=_mc, matched_articles_count=len(results),
        had_expanded_query=False,
    ))
    await service.db.commit()

    return results


@router.post("/legal-advice", response_model=LegalAdviceResponse)
async def legal_advice(
    req: LegalAdviceRequest,
    service: LegislationService = Depends(get_service),
):
    from app.schemas.legislation import ArticleExcerpt

    async def _log_full(
        status: str, ep: str = "consult",
        confidence: int = 0, completeness: int = 100,
        pairs_data: list | None = None, article_excerpts_data: list | None = None,
        analysis_text: str | None = None, had_expanded: bool = False,
    ) -> None:
        matched_laws = None
        matched_categories = None
        matched_articles_count = 0
        if pairs_data:
            law_list = []
            cat_set: set[str] = set()
            for law_obj, art_list in pairs_data:
                cat = law_obj.category.name if hasattr(law_obj, 'category') and law_obj.category else ""
                law_list.append({"id": law_obj.id, "title": law_obj.title, "number": law_obj.number, "category": cat})
                if cat:
                    cat_set.add(cat)
                matched_articles_count += len(art_list)
            matched_laws = json.dumps(law_list, ensure_ascii=False)
            matched_categories = json.dumps(list(cat_set), ensure_ascii=False)
        if article_excerpts_data is not None:
            matched_articles_count = len(article_excerpts_data)

        db_log = service.db
        db_log.add(LegalAdviceLog(
            situation=req.situation,
            client_type=req.client_type,
            search_method=req.search_method or "auto",
            endpoint=ep,
            status=status,
            completeness=completeness,
            confidence=confidence,
            matched_laws=matched_laws,
            matched_categories=matched_categories,
            matched_articles_count=matched_articles_count,
            analysis_length=len(analysis_text) if analysis_text else None,
            had_expanded_query=had_expanded,
        ))
        await db_log.commit()

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

    # --- Build results and excerpts ---
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

    # --- Completeness check ---
    llm_suggestion = ""
    if pairs:
        condition_map: dict[int, list[ArticleCondition]] = {}
        article_count = 0
        for law, articles in pairs:
            for art in articles:
                if article_count >= MAX_CONDITION_ARTICLES:
                    break
                conditions = await _get_or_create_conditions(art, service.db)
                if conditions:
                    condition_map[art.id] = conditions
                article_count += 1

        articles_text = _format_articles_with_conditions(pairs, condition_map)

        if condition_map:
            completeness_result = await check_completeness(search_text, articles_text, req.client_type)
            completeness = completeness_result["completeness"]

            if completeness < 100:
                await _log_full("FAIL", confidence=completeness_result["confidence"], completeness=completeness, pairs_data=pairs, had_expanded=bool(expanded_query))
                return LegalAdviceResponse(
                    status="FAIL",
                    completeness=completeness,
                    confidence=completeness_result["confidence"],
                    instruction=completeness_result["instruction"],
                    suggestion=completeness_result["suggestion"],
                    clarifying_questions=completeness_result["clarifying_questions"],
                    relevant_laws=results,
                    relevant_articles=article_excerpts,
                    client_type=req.client_type,
                )
            llm_suggestion = completeness_result.get("suggestion") or ""

    # --- Full analysis (completeness = 100 or no conditions) ---
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

    await _log_full("PASS", confidence=confidence, pairs_data=pairs, article_excerpts_data=article_excerpts, analysis_text=analysis, had_expanded=bool(expanded_query))
    return LegalAdviceResponse(
        status="PASS",
        completeness=100,
        confidence=confidence,
        suggestion=llm_suggestion,
        analysis=analysis,
        relevant_laws=results,
        relevant_articles=article_excerpts,
        expanded_query=expanded_query,
        refinement_hint=refinement_hint,
        control_question=control_question,
        client_type=req.client_type,
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


def _sse_progress(percent: int, message: str, level: str = "info") -> str:
    return f"data: {json.dumps({'type': 'progress', 'percent': percent, 'message': message, 'level': level}, ensure_ascii=False)}\n\n"


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
                    yield _sse_progress(5, "Загрузка списка нормативных актов...")
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

                # --- Completeness check ---
                llm_suggestion = ""
                if pairs:
                    yield _sse_progress(45, "Проверка условий применения статей...")
                    condition_map: dict[int, list[ArticleCondition]] = {}
                    article_count = 0
                    for law, articles in pairs:
                        for art in articles:
                            if article_count >= MAX_CONDITION_ARTICLES:
                                break
                            conditions = await _get_or_create_conditions(art, db)
                            if conditions:
                                condition_map[art.id] = conditions
                            article_count += 1

                    articles_text = _format_articles_with_conditions(pairs, condition_map)

                    if condition_map:
                        retry_warnings: list[str] = []
                        async def _retry_warn(msg: str) -> None:
                            retry_warnings.append(msg)
                        completeness_result = await check_completeness(
                            search_text, articles_text, req.client_type,
                            progress_callback=_retry_warn,
                        )
                        for w in retry_warnings:
                            yield _sse_progress(46, w, "warning")
                        completeness = completeness_result["completeness"]

                        if completeness < 100:
                            result = LegalAdviceResponse(
                                status="FAIL",
                                completeness=completeness,
                                confidence=completeness_result["confidence"],
                                instruction=completeness_result["instruction"],
                                suggestion=completeness_result["suggestion"],
                                clarifying_questions=completeness_result["clarifying_questions"],
                                relevant_laws=results,
                                relevant_articles=article_excerpts,
                                client_type=req.client_type,
                            )
                            yield _sse_progress(100, "Требуются уточнения")
                            _ml = json.dumps([{"id": r.id, "title": r.title, "number": r.number, "category": r.category_name} for r in results], ensure_ascii=False) if results else None
                            _mc = json.dumps(list({r.category_name for r in results if r.category_name}), ensure_ascii=False) if results else None
                            _ac = sum(len(a) for _, a in pairs) if pairs else 0
                            db.add(LegalAdviceLog(situation=search_text, client_type=req.client_type, status="FAIL", confidence=completeness_result["confidence"], completeness=completeness, search_method=req.search_method or "auto", endpoint="consult", matched_laws=_ml, matched_categories=_mc, matched_articles_count=_ac, had_expanded_query=bool(expanded_query)))
                            await db.commit()
                            yield _sse_event("result", result.model_dump(mode="json"))
                            return
                        llm_suggestion = completeness_result.get("suggestion") or ""

                yield _sse_progress(50, "Подготовка данных для анализа...")
                yield _sse_progress(55, "Извлечение юридических фактов...")
                yield _sse_progress(60, "Применение норм права к ситуации...")
                yield _sse_progress(63, "Построение правового заключения...")
                control_question = None
                analysis = await generate_legal_analysis(search_text, pairs)
                if analysis:
                    control_question = _extract_control_question(analysis)

                yield _sse_progress(70, "Запуск самопроверки...")
                yield _sse_progress(75, "Проверка фактов (самопроверка)...")
                yield _sse_progress(80, "Поиск контраргументов...")
                verification = await cove_verify(search_text, analysis, pairs)
                if verification.get("analysis"):
                    analysis = verification["analysis"]
                    control_question = _extract_control_question(analysis) or control_question
                if verification.get("confidence", 0) > 0:
                    confidence = verification["confidence"]

                yield _sse_progress(85, "Верификация цитат...")
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
                    status="PASS",
                    completeness=100,
                    confidence=confidence,
                    suggestion=llm_suggestion,
                    analysis=analysis,
                    relevant_laws=results,
                    relevant_articles=article_excerpts,
                    expanded_query=expanded_query,
                    refinement_hint=refinement_hint,
                    control_question=control_question,
                    client_type=req.client_type,
                )

                _ml = json.dumps([{"id": r.id, "title": r.title, "number": r.number, "category": r.category_name} for r in results], ensure_ascii=False) if results else None
                _mc = json.dumps(list({r.category_name for r in results if r.category_name}), ensure_ascii=False) if results else None
                _ac = len(article_excerpts) if article_excerpts else (sum(len(a) for _, a in pairs) if pairs else 0)
                db.add(LegalAdviceLog(situation=search_text, client_type=req.client_type, status="PASS", confidence=confidence, completeness=100, search_method=req.search_method or "auto", endpoint="consult", matched_laws=_ml, matched_categories=_mc, matched_articles_count=_ac, analysis_length=len(analysis) if analysis else None, had_expanded_query=bool(expanded_query)))
                await db.commit()

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


@router.get("/legal-advice/stats")
async def legal_advice_stats(service: LegislationService = Depends(get_service)):
    from sqlalchemy import func, select as sa_select
    total = await service.db.scalar(sa_select(func.count(LegalAdviceLog.id))) or 0
    consults = await service.db.scalar(sa_select(func.count(LegalAdviceLog.id)).where(LegalAdviceLog.endpoint == "consult")) or 0
    searches = await service.db.scalar(sa_select(func.count(LegalAdviceLog.id)).where(LegalAdviceLog.endpoint == "search")) or 0
    fails = await service.db.scalar(sa_select(func.count(LegalAdviceLog.id)).where(LegalAdviceLog.status == "FAIL")) or 0

    rows = await service.db.execute(
        sa_select(LegalAdviceLog.matched_categories).where(
            LegalAdviceLog.matched_categories.isnot(None),
            LegalAdviceLog.endpoint == "consult",
            LegalAdviceLog.confidence >= 50,
        ).limit(200)
    )
    from collections import Counter
    cat_counter: Counter = Counter()
    for (row,) in rows:
        try:
            cats = json.loads(row)
            if cats:
                cat_counter[cats[0]] += 1
        except Exception:
            pass

    top_categories = [{"name": k, "count": v} for k, v in cat_counter.most_common(10)]

    return {
        "total_requests": total,
        "consultations": consults,
        "searches": searches,
        "fails": fails,
        "top_categories": top_categories,
    }
