from sqlalchemy import select, or_, and_, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.legislation import Category, Law, Article, LawStatus

_STOP_WORDS = frozenset({
    "и", "в", "не", "на", "с", "по", "от", "за", "о", "для", "до",
    "при", "из", "у", "к", "а", "но", "то", "же", "бы", "ли", "как",
    "что", "это", "все", "или", "его", "ее", "их", "если", "чтобы",
    "который", "которая", "которые", "которого", "которой",
    "также", "может", "могут", "должен", "должна", "должны",
    "менее", "более", "самое", "самого", "самой",
    "время", "случае", "случай", "срок", "порядок",
    "основание", "основания", "положение", "положения",
    "часть", "части", "пункт", "пункта", "пункты",
    "раздел", "раздела", "глава", "главы", "статья", "статьи",
    "настоящий", "настоящего", "настоящей",
    "соответствие", "соответствии",
    "отношение", "отношения", "отношении",
    "далее", "также", "будет", "будут",
    "течение", "течения",
    "норма", "нормы", "норм",
    "действие", "действия", "действий",
    "общий", "общего", "общей", "общие",
    "орган", "органа", "органы", "органов",
    "прочий", "прочие", "прочего",
    "число", "числа", "числе",
    "место", "места", "месте",
    "первый", "первого", "первой",
    "второй", "третий",
    "данный", "данного", "данной", "данные",
    "работник", "работника", "работники",
    "размер", "размера",
    "форма", "формы", "форме",
    "условие", "условия", "условий",
    "цель", "цели", "целей", "целях",
    "подлежит", "подлежат",
    "уполномоченный", "уполномоченного",
    "информация", "информации",
    "документ", "документа", "документы", "документов",
    "год", "года", "году", "лет",
    "день", "дня", "дней",
    "лицо", "лица", "лиц",
    "орган", "органа",
    "область", "области",
    "территория", "территории",
    "организация", "организации", "организаций",
    "деятельность", "деятельности",
    "требование", "требования", "требований",
    "осуществление", "осуществления",
    "предусмотренный", "предусмотренного",
    "соответствующий", "соответствующего",
    "установленный", "установленного",
    "государственный", "государственного",
    "местный", "местного",
    "ночное", "время",
})


def _tokenize(text: str) -> list[str]:
    import re
    words = re.findall(r"[а-яёa-z]+", text.lower())
    return [w for w in words if len(w) > 2 and w not in _STOP_WORDS]


class LegislationService:

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_categories(self) -> list[Category]:
        result = await self.db.execute(
            select(Category)
            .options(selectinload(Category.children))
            .order_by(Category.sort_order)
        )
        return list(result.scalars().all())

    async def get_category_tree(self) -> list[Category]:
        result = await self.db.execute(
            select(Category)
            .options(selectinload(Category.children))
            .where(Category.parent_id.is_(None))
            .order_by(Category.sort_order)
        )
        return list(result.scalars().all())

    async def get_category_by_slug(self, slug: str) -> Category | None:
        result = await self.db.execute(
            select(Category)
            .options(selectinload(Category.laws).selectinload(Law.articles))
            .where(Category.slug == slug)
        )
        return result.scalar_one_or_none()

    async def get_law(self, law_id: int) -> Law | None:
        result = await self.db.execute(
            select(Law)
            .options(selectinload(Law.articles), selectinload(Law.category))
            .where(Law.id == law_id)
        )
        return result.scalar_one_or_none()

    async def search_laws(self, query: str, limit: int = 20) -> list[Law]:
        q = query.lower().strip()
        stmt = select(Law).options(
            selectinload(Law.category), selectinload(Law.articles)
        )
        result = await self.db.execute(stmt)
        laws = list(result.scalars().all())

        # Exact substring search first
        matched = []
        for law in laws:
            if q in (law.title or "").lower():
                matched.append(law)
                continue
            if q in (law.full_text or "").lower():
                matched.append(law)
                continue
            if q in (law.summary or "").lower():
                matched.append(law)
                continue
            if law.number and q in law.number.lower():
                matched.append(law)
                continue
            for article in law.articles:
                if q in (article.content or "").lower() or q in (article.title or "").lower():
                    matched.append(law)
                    break

        if matched:
            return matched[:limit]

        # Semantic fallback: tokenize query, score laws by word overlap
        words = _tokenize(q)
        if not words:
            return []

        scored = []
        for law in laws:
            score = 0
            text = f"{law.title or ''} {law.full_text or ''} {law.summary or ''}".lower()
            for w in words:
                if w in text:
                    score += 2
            for article in law.articles:
                art_text = f"{article.title or ''} {article.content or ''}".lower()
                for w in words:
                    if w in art_text:
                        score += 1
            if score > 0:
                scored.append((score, law))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [law for _, law in scored[:limit]]

    async def search_articles(self, query: str, limit: int = 20) -> list[Article]:
        q = query.lower()
        stmt = select(Article).options(
            selectinload(Article.law).selectinload(Law.category)
        )
        result = await self.db.execute(stmt)
        all_articles = list(result.scalars().all())

        matched = []
        for article in all_articles:
            if q in (article.content or "").lower() or q in (article.title or "").lower():
                matched.append(article)
                if len(matched) >= limit:
                    break

        return matched

    @staticmethod
    def _match_key(token: str) -> str:
        """Return the first 5 chars of a token for prefix matching.
        Handles Russian inflection: 'покупка' → 'покуп' matches 'покупки', 'покупку', etc."""
        return token[:5] if len(token) >= 5 else token

    @staticmethod
    def _common_prefix_score(token: str, text: str) -> int:
        """Score how well token matches text based on longest common prefix
        with any word in text. Higher = more specific match.
        - 15: exact token substring match
        - 9-14: common prefix of 9-14 chars (very close morph variant)
        - 5-8: common prefix of 5-8 chars (weak morph match)
        - 0: no meaningful match
        """
        token_len = len(token)
        if token in text:
            return 15
        for word in text.split():
            word_len = len(word)
            max_check = min(token_len, word_len, 15)
            for i in range(max_check, 4, -1):
                if token[:i] == word[:i]:
                    return i
        return 0

    async def find_relevant_articles(
        self, situation: str, max_per_law: int = 5, total_limit: int = 25
    ) -> list[tuple[Law, list[Article]]]:
        import re

        tokens = [w for w in re.findall(r'[а-яёa-z]+', situation.lower())
                  if len(w) > 2 and w not in _STOP_WORDS]
        if not tokens:
            return []

        token_keys = {t: self._match_key(t) for t in tokens}
        strong_tokens = [t for t in tokens if len(t) >= 5] or tokens

        stmt = select(Law).options(
            selectinload(Law.category), selectinload(Law.articles)
        ).where(Law.status.in_([LawStatus.ACTIVE, LawStatus.AMENDED]))
        result = await self.db.execute(stmt)
        all_laws = list(result.scalars().all())

        scored_pairs = []
        for law in all_laws:
            title_lower = (law.title or "").lower()
            cat_lower = (law.category.name or "").lower() if law.category else ""

            matched_tokens = set()
            matched_strong_keys = set()
            matched_articles = []
            has_title_match = False

            # First pass: determine law relevance (token exists anywhere)
            for token in tokens:
                key = token_keys[token]
                ts = self._common_prefix_score(token, title_lower)
                if ts >= 7:
                    has_title_match = True
                    matched_tokens.add(token)
                    if len(token) >= 5:
                        matched_strong_keys.add(token)
                    continue

                if ts >= 5 or (cat_lower and self._common_prefix_score(token, cat_lower) >= 5):
                    matched_tokens.add(token)
                    if len(token) >= 5:
                        matched_strong_keys.add(token)
                    continue

                # Check if token exists in any article
                for article in (law.articles or []):
                    text = ((article.content or "") + " " + (article.title or "")).lower()
                    if key in text:
                        matched_tokens.add(token)
                        if len(token) >= 5:
                            matched_strong_keys.add(token)
                        break

            if not matched_tokens:
                continue

            # Coverage: title match (ts>=7) auto-passes;
            # short strong-queries (1-2 strong tokens) need ALL strong keys to match;
            # longer strong-queries need at least 2 strong keys in articles
            if not has_title_match and strong_tokens:
                if len(strong_tokens) <= 2:
                    if len(matched_strong_keys) < len(strong_tokens):
                        continue
                elif len(matched_strong_keys) < 2:
                    continue

            # Second pass: score all articles, take top max_per_law by relevance
            scored_articles = []
            for article in (law.articles or []):
                title = (article.title or "").lower()
                content = (article.content or "").lower()

                title_score = sum(self._common_prefix_score(t, title) for t in tokens)
                content_matches = sum(1 for t in tokens if token_keys[t] in content)

                if title_score >= 5 or content_matches >= 2:
                    scored_articles.append((title_score + content_matches * 3, article))

            scored_articles.sort(key=lambda x: x[0], reverse=True)
            matched_articles = [art for _, art in scored_articles[:max_per_law]]

            # Score: title common-prefix, category common-prefix, articles, distinct token coverage
            title_bonus = sum(self._common_prefix_score(t, title_lower) for t in tokens)
            cat_bonus = sum(self._common_prefix_score(t, cat_lower) for t in tokens) if cat_lower else 0
            article_bonus = len(matched_articles) * 2
            token_coverage_bonus = len(matched_tokens) * 3
            score = title_bonus + cat_bonus + article_bonus + token_coverage_bonus

            scored_pairs.append((score, law, matched_articles))

        scored_pairs.sort(key=lambda x: (x[0], len(x[2])), reverse=True)
        return [(law, arts) for _, law, arts in scored_pairs[:total_limit]]
