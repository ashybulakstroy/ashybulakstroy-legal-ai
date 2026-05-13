from sqladmin import Admin, ModelView

from app.database import engine, async_session_factory
from app.models.legislation import Category, Law, Article
from app.config import settings


class CategoryAdmin(ModelView, model=Category):
    name = "Категория"
    name_plural = "Категории НПА"
    icon = "fa-folder"
    column_list = ["id", "name", "slug", "type", "parent", "sort_order"]
    column_searchable_list = ["name", "slug"]
    column_sortable_list = ["name", "type", "sort_order"]
    form_excluded_columns = ["laws", "children", "created_at"]


class LawAdmin(ModelView, model=Law):
    name = "Закон"
    name_plural = "Законы / НПА"
    icon = "fa-gavel"
    column_list = ["id", "title", "number", "category", "status", "date_adopted", "date_effective"]
    column_searchable_list = ["title", "number", "summary"]
    column_sortable_list = ["title", "date_adopted", "status"]
    form_excluded_columns = ["articles", "created_at", "updated_at"]
    form_overrides = {"full_text": None}


class ArticleAdmin(ModelView, model=Article):
    name = "Статья"
    name_plural = "Статьи"
    icon = "fa-file-text"
    column_list = ["id", "law", "number", "title"]
    column_searchable_list = ["title", "content"]
    column_sortable_list = ["law", "number"]
    form_excluded_columns = ["created_at"]


def setup_admin(app):
    admin = Admin(app, engine=engine,
                  title="Законодательство РК — Админ",
                  base_url="/admin")
    admin.add_view(CategoryAdmin)
    admin.add_view(LawAdmin)
    admin.add_view(ArticleAdmin)
    return admin
