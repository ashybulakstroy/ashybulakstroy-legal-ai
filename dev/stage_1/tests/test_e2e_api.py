"""End-to-end test for POST /api/v1/legal-advice endpoint."""
import asyncio
import pytest

pytestmark = pytest.mark.asyncio(loop_scope="module")

from httpx import AsyncClient, ASGITransport
from app.main import app


@pytest.fixture(scope="module")
def client():
    """Create an async test client for the FastAPI app."""
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test")
    yield client
    asyncio.run(client.aclose())


def test_legal_advice_basic_structure(client):
    """Verify POST /api/v1/legal-advice returns correct JSON structure."""
    async def run():
        response = await client.post("/api/v1/legal-advice", json={
            "situation": "недостаток товара",
        })
        assert response.status_code == 200
        data = response.json()

        # Top-level fields
        assert "situation" in data
        assert "relevant_laws" in data
        assert "relevant_articles" in data
        assert "analysis" in data
        assert "disclaimer" in data

        assert data["situation"] == "недостаток товара"
        assert isinstance(data["relevant_laws"], list)
        assert isinstance(data["relevant_articles"], list)
        assert isinstance(data["analysis"], str)
        assert len(data["analysis"]) > 0
        assert isinstance(data["disclaimer"], str)

        # relevant_laws items
        if data["relevant_laws"]:
            law = data["relevant_laws"][0]
            assert "id" in law
            assert "title" in law
            assert "status" in law
            assert "category_name" in law
            assert law["id"] > 0
            assert isinstance(law["title"], str)
            assert len(law["title"]) > 0

        # relevant_articles items
        if data["relevant_articles"]:
            art = data["relevant_articles"][0]
            assert "id" in art
            assert "number" in art
            assert "content" in art
            assert "law_id" in art
            assert "law_title" in art

        return data

    return asyncio.run(run())


def test_legal_advice_with_context(client):
    """Verify context parameter is accepted."""
    async def run():
        response = await client.post("/api/v1/legal-advice", json={
            "situation": "нарушение тишины",
            "context": "соседи шумят после 23:00",
        })
        assert response.status_code == 200
        data = response.json()
        assert data["situation"] == "нарушение тишины"
        return data

    return asyncio.run(run())


def test_legal_advice_refresh(client):
    """Verify refresh=True is accepted."""
    async def run():
        response = await client.post("/api/v1/legal-advice", json={
            "situation": "увольнение",
            "refresh": True,
        })
        assert response.status_code == 200
        return response.json()

    return asyncio.run(run())


def test_legal_advice_no_results(client):
    """Verify response with no matching laws still has valid structure."""
    async def run():
        response = await client.post("/api/v1/legal-advice", json={
            "situation": "квантовая физика в рк",
        })
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data["relevant_laws"], list)
        assert isinstance(data["relevant_articles"], list)
        assert isinstance(data["analysis"], str)
        return data

    return asyncio.run(run())


def test_legal_advice_empty_situation_ok(client):
    """Verify empty situation still returns 200 (valid structure)."""
    async def run():
        response = await client.post("/api/v1/legal-advice", json={
            "situation": "",
        })
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data["relevant_laws"], list)
        assert isinstance(data["analysis"], str)
        return data

    return asyncio.run(run())
