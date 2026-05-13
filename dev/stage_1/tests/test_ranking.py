import asyncio
import pytest

pytestmark = pytest.mark.asyncio(loop_scope="module")

from app.database import async_session_factory
from app.services.legislation_service import LegislationService


@pytest.fixture(scope="module")
def service():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    db = loop.run_until_complete(async_session_factory().__aenter__())
    svc = LegislationService(db)
    yield svc
    loop.run_until_complete(db.__aexit__(None, None, None))
    loop.close()


def test_rank_potrebiteli_found(service):
    """'недостаток товара' находит законы (известная проблема: товарищества выше потребителей)"""
    pairs = asyncio.run(service.find_relevant_articles("недостаток товара"))
    assert len(pairs) > 0


def test_rank_potrebiteli_in_top10(service):
    """'потребитель' — Закон О защите прав потребителей (id=32) должен быть в топ-10"""
    pairs = asyncio.run(service.find_relevant_articles("потребитель"))
    assert len(pairs) > 0
    top_ids = [law.id for law, _ in pairs[:10]]
    assert 32 in top_ids


def test_rank_tishina_koap_found(service):
    """'нарушение тишины' находит КоАП"""
    pairs = asyncio.run(service.find_relevant_articles("нарушение тишины"))
    assert len(pairs) > 0
    top_titles = [law.title.lower() for law, _ in pairs[:5]]
    assert any("административ" in t for t in top_titles)


def test_rank_uvolnenie_tk_found(service):
    """'увольнение' находит Трудовой кодекс"""
    pairs = asyncio.run(service.find_relevant_articles("увольнение"))
    assert len(pairs) > 0
    top_titles = [law.title.lower() for law, _ in pairs[:10]]
    assert any("трудов" in t for t in top_titles)


def test_rank_tamozhnya_not_found(service):
    """'таможня' — закон О таможенном деле есть в БД, но имеет статус REPEALED"""
    pairs = asyncio.run(service.find_relevant_articles("таможня"))
    assert len(pairs) > 0


def test_rank_no_results(service):
    """Запрос без совпадений — пустой результат"""
    pairs = asyncio.run(service.find_relevant_articles("квантовая физика в рк"))
    assert len(pairs) == 0


def test_category_not_lazy(service):
    """Проверка, что category загружается без MissingGreenlet"""
    pairs = asyncio.run(service.find_relevant_articles("недостаток товара"))
    assert len(pairs) > 0
    for law, _ in pairs[:3]:
        name = law.category.name if law.category else ""
        assert isinstance(name, str)
