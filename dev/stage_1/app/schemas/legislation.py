from datetime import date, datetime
from pydantic import BaseModel


class ArticleResponse(BaseModel):
    id: int
    number: str | None
    title: str | None
    content: str | None
    sort_order: int

    model_config = {"from_attributes": True}


class LawResponse(BaseModel):
    id: int
    category_id: int
    title: str
    number: str | None
    date_adopted: date | None
    date_effective: date | None
    date_expired: date | None
    status: str
    summary: str | None
    full_text: str | None
    created_at: datetime
    updated_at: datetime
    articles: list[ArticleResponse] = []

    model_config = {"from_attributes": True}


class LawListItem(BaseModel):
    id: int
    category_id: int
    title: str
    number: str | None
    date_adopted: date | None
    status: str
    summary: str | None

    model_config = {"from_attributes": True}


class CategoryResponse(BaseModel):
    id: int
    name: str
    slug: str
    description: str | None
    type: str
    parent_id: int | None
    sort_order: int
    laws: list[LawListItem] = []

    model_config = {"from_attributes": True}


class CategoryTree(CategoryResponse):
    children: list["CategoryTree"] = []


class SearchResult(BaseModel):
    id: int
    title: str
    number: str | None
    summary: str | None
    status: str
    category_name: str
    score: float
    match_count: int = 0

    model_config = {"from_attributes": True}


class ArticleExcerpt(BaseModel):
    id: int
    number: str | None
    title: str | None
    content: str | None
    law_id: int
    law_title: str


class LegalAdviceRequest(BaseModel):
    situation: str
    context: str | None = None
    search_method: str = "llm"
    refresh: bool = False


class SelectionResult(BaseModel):
    law_title: str | None = None
    article_number: str | None = None
    confidence: int = 0
    reasoning: str | None = None


class LegalAdviceResponse(BaseModel):
    situation: str
    relevant_laws: list[SearchResult]
    relevant_articles: list[ArticleExcerpt] = []
    analysis: str
    expanded_query: str | None = None
    refinement_hint: str | None = None
    confidence: int = 0
    control_question: str | None = None
    disclaimer: str = "Данная информация носит справочный характер. Для получения официальной консультации обратитесь к квалифицированному юристу."
