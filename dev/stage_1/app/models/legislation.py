from datetime import date, datetime

from sqlalchemy import ForeignKey, Integer, String, Text, Date, DateTime, Boolean, Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

import enum


class CategoryType(str, enum.Enum):
    CONSTITUTION = "constitution"
    CODEX = "codex"
    LAW = "law"
    DECREE = "decree"
    RESOLUTION = "resolution"
    ORDER = "order"
    OTHER = "other"


class LawStatus(str, enum.Enum):
    ACTIVE = "active"
    AMENDED = "amended"
    REPEALED = "repealed"
    DRAFT = "draft"


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    type: Mapped[CategoryType] = mapped_column(SAEnum(CategoryType), nullable=False)
    parent_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("categories.id"), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    parent: Mapped["Category | None"] = relationship("Category", remote_side="Category.id", back_populates="children")
    children: Mapped[list["Category"]] = relationship("Category", back_populates="parent")
    laws: Mapped[list["Law"]] = relationship("Law", back_populates="category")

    def __repr__(self) -> str:
        return f"<Category {self.name}>"


class Law(Base):
    __tablename__ = "laws"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category_id: Mapped[int] = mapped_column(Integer, ForeignKey("categories.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    date_adopted: Mapped[date | None] = mapped_column(Date, nullable=True)
    date_effective: Mapped[date | None] = mapped_column(Date, nullable=True)
    date_expired: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[LawStatus] = mapped_column(SAEnum(LawStatus), default=LawStatus.ACTIVE)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    full_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    category: Mapped["Category"] = relationship("Category", back_populates="laws")
    articles: Mapped[list["Article"]] = relationship("Article", back_populates="law", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Law {self.title}>"


class Article(Base):
    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    law_id: Mapped[int] = mapped_column(Integer, ForeignKey("laws.id"), nullable=False)
    number: Mapped[str | None] = mapped_column(String(50), nullable=True)
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    law: Mapped["Law"] = relationship("Law", back_populates="articles")
    conditions: Mapped[list["ArticleCondition"]] = relationship("ArticleCondition", back_populates="article", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Article {self.law_id}.{self.number}>"


class QueryExpansionCache(Base):
    __tablename__ = "query_expansion_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    original_query: Mapped[str] = mapped_column(String(500), nullable=False, unique=True)
    expanded_query: Mapped[str] = mapped_column(String(500), nullable=False)
    confidence: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def __repr__(self) -> str:
        return f"<QueryExpansionCache {self.original_query!r} -> {self.expanded_query!r}>"


class ArticleCondition(Base):
    __tablename__ = "article_conditions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    article_id: Mapped[int] = mapped_column(Integer, ForeignKey("articles.id"), nullable=False)
    condition_text: Mapped[str] = mapped_column(String(500), nullable=False)
    condition_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    is_required: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    article: Mapped["Article"] = relationship("Article", back_populates="conditions")

    def __repr__(self) -> str:
        return f"<ArticleCondition #{self.id}: {self.condition_text[:60]}>"


class SearchCache(Base):
    __tablename__ = "search_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    search_text: Mapped[str] = mapped_column(String(500), nullable=False, unique=True)
    result_json: Mapped[str] = mapped_column(Text, nullable=False)
    cache_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def __repr__(self) -> str:
        return f"<SearchCache {self.search_text!r} (v{self.cache_version})>"


class LegalAdviceLog(Base):
    __tablename__ = "legal_advice_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    situation: Mapped[str] = mapped_column(Text, nullable=False)
    client_type: Mapped[str] = mapped_column(String(50), default="forms")
    search_method: Mapped[str] = mapped_column(String(10), default="auto")
    endpoint: Mapped[str] = mapped_column(String(20), nullable=False, default="consult")

    status: Mapped[str] = mapped_column(String(10), default="PASS")
    completeness: Mapped[int] = mapped_column(Integer, default=100)
    confidence: Mapped[int] = mapped_column(Integer, default=0)

    matched_laws: Mapped[str | None] = mapped_column(Text, nullable=True)
    matched_categories: Mapped[str | None] = mapped_column(Text, nullable=True)
    matched_articles_count: Mapped[int] = mapped_column(Integer, default=0)

    analysis_length: Mapped[int | None] = mapped_column(Integer, nullable=True)
    had_expanded_query: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def __repr__(self) -> str:
        return f"<LegalAdviceLog #{self.id} [{self.status}] {self.endpoint}>"
