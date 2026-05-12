<div align="center">

# ⚖️ Legal AI Agent / Правовой Навигатор

**AI-ассистент для бизнеса и граждан Республики Казахстан**  
**AI assistant for businesses and citizens of the Republic of Kazakhstan**

[![Python](https://img.shields.io/badge/Python-3.12+-3776AB?logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![React](https://img.shields.io/badge/React-18-61DAFB?logo=react&logoColor=white)](https://react.dev)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169E1?logo=postgresql&logoColor=white)](https://postgresql.org)
[![Neo4j](https://img.shields.io/badge/Neo4j-5-008CC1?logo=neo4j&logoColor=white)](https://neo4j.com)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

</div>

---

## 📋 О проекте | About

**Правовой Навигатор** — открытая AI-система, которая преобразует описание ситуации клиента в юридический путь решения с использованием законодательных инструментов Республики Казахстан.

**Legal AI Agent** is an open AI system that transforms a client's situation description into a legal solution path using the legislative instruments of the Republic of Kazakhstan.

### Ключевые возможности | Key Features

- 🧠 **Multi-agent pipeline** — 7 последовательных AI-агентов (Router → Legal Expert → Statute Researcher → Case Law Researcher → Adversarial Verifier → Strategist → Document Drafter)
- 📚 **Multi-source ingestion** — парсинг и кросс-валидация данных из adilet.zan.kz, zan.gov.kz, zan.kz, data.egov.kz, sud.kz
- 🔍 **Hybrid RAG** — dense embeddings + BM25 + knowledge graph + reranker + quality-level ranking
- 🏷️ **4-level Quality Gate** — GOLD / SILVER / BRONZE / UNVERIFIED для каждого документа
- 🕵️ **DeepVerifier** — 3-осевая верификация: точность, релевантность, соответствие законодательству
- 🔐 **Adversarial Self-Check** — встроенный "Kill Switch" для проверки собственных выводов
- 👥 **5 user priority levels** — от сотрудника розницы до внешнего пользователя
- 🌐 **SSR/SSG** — SEO-видимость для публичных страниц
- 🤖 **AI-compatible API** — OpenAI-compatible, MCP (Model Context Protocol), ChatGPT Actions, Gemini Function Calling, WebSocket, webhooks
- 📊 **Marketing analytics** — event tracking, consent management, content suggestion engine

---

## 🏗️ Архитектура | Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      Frontend (React + Vite)                │
│  SSR/SSG (public)  │  SPA (authenticated)  │  i18n (RU/KZ/EN)│
└──────────────────────────┬──────────────────────────────────┘
                           │ HTTP / WebSocket
┌──────────────────────────▼──────────────────────────────────┐
│                   Backend (FastAPI)                         │
│  ┌─────────┐ ┌──────────┐ ┌──────────┐ ┌────────────────┐  │
│  │  Auth   │ │  Agents  │ │   MCP    │ │  SEO + Marketing│  │
│  └─────────┘ └──────────┘ └──────────┘ └────────────────┘  │
│  ┌─────────┐ ┌──────────┐ ┌──────────┐ ┌────────────────┐  │
│  │  RAG    │ │  Graph   │ │Ingestion │ │  Guardrails    │  │
│  └─────────┘ └──────────┘ └──────────┘ └────────────────┘  │
└───────┬─────────────┬──────────────┬───────────────────────┘
        │              │              │
┌───────▼──────┐ ┌─────▼──────┐ ┌─────▼──────────────────┐
│  PostgreSQL  │ │   Neo4j    │ │  Qdrant / ChromaDB     │
│  (структура) │ │  (графы)   │ │  (векторы)             │
└──────────────┘ └────────────┘ └────────────────────────┘
```

### Agent Pipeline

```
Client Input
    │
    ▼
┌──────────┐   ┌──────────┐   ┌──────────────┐   ┌───────────────┐
│  Router  │──▶│  Legal   │──▶│   Statute    │──▶│  Case Law     │
│ (domain) │   │  Expert  │   │  Researcher  │   │  Researcher   │
└──────────┘   └──────────┘   └──────────────┘   └───────────────┘
                                                       │
                                                       ▼
┌──────────────┐   ┌──────────┐   ┌────────────────┐   │
│  Document    │◀──│Strategist│◀──│  Adversarial   │◀──┘
│  Drafter     │   │          │   │   Verifier     │
└──────────────┘   └──────────┘   └────────────────┘
```

---

## 🚀 Быстрый старт | Quick Start

### Требования | Prerequisites

- Python 3.12+
- Node.js 20+
- PostgreSQL 16

### Локальный запуск | Local Setup

```bash
# 1. Клонировать | Clone
git clone https://github.com/your-org/legal-ai-agent.git
cd legal-ai-agent

# 2. Настройка окружения | Environment setup
cp .env.example .env
# Отредактируйте .env — укажите LLM_API_KEY и параметры БД

# 3. Backend
cd backend
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
# .venv\Scripts\activate   # Windows
pip install -e ".[dev]"
alembic upgrade head
uvicorn app.main:app --reload --port 8000

# 4. Frontend (в новом терминале)
cd frontend
npm install
npm run dev

# 5. Открыть браузер | Open browser
# http://localhost:5173
# http://localhost:8000/docs — Swagger
```

---

## 📁 Структура проекта | Project Structure

```
legal-ai-agent/
├── backend/
│   ├── app/
│   │   ├── api/             # API endpoints (v1)
│   │   ├── core/            # Config, security
│   │   ├── models/          # SQLAlchemy models
│   │   ├── schemas/         # Pydantic schemas
│   │   ├── services/        # Business logic
│   │   ├── db/              # Database session
│   │   ├── mcp/             # MCP Server
│   │   ├── agents/          # AI agents
│   │   ├── rag/             # RAG pipeline
│   │   ├── graph/           # Knowledge graph
│   │   ├── ingestion/       # Data ingestion
│   │   └── guardrails/      # Input/output guardrails
│   ├── alembic/             # DB migrations
│   ├── tests/               # Tests
│   └── pyproject.toml
├── frontend/
│   ├── src/
│   │   ├── components/      # React components
│   │   ├── pages/           # Page components
│   │   ├── api/             # API client
│   │   ├── i18n/            # Internationalization
│   │   └── hooks/           # Custom hooks
│   └── package.json
├── .env.example
└── README.md
```

---

## 🛠️ API endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health check |
| `GET` | `/openapi.json` | OpenAPI spec (AI-compatible) |
| `GET` | `/.well-known/ai-plugin.json` | ChatGPT Actions manifest |
| `GET` | `/api/v1/compat/openai/models` | OpenAI-compatible models list |
| `POST` | `/api/v1/compat/openai/chat/completions` | OpenAI-compatible chat |
| `POST` | `/api/v1/compat/openai/embeddings` | OpenAI-compatible embeddings |
| `POST` | `/api/v1/mcp/query` | MCP tool execution |
| `GET` | `/api/v1/mcp/sse` | MCP SSE transport |
| `GET` | `/api/v1/seo/sitemap` | SEO sitemap |
| `POST` | `/api/v1/webhooks/register` | Register webhook |
| ... | ... | (20+ endpoints total) |

---

## 🧪 Технологический стек | Tech Stack

| Category | Technologies |
|----------|-------------|
| **Backend** | Python 3.12+, FastAPI, SQLAlchemy 2.0, Alembic |
| **Frontend** | React 18, TypeScript, Vite, TanStack Query, React Router |
| **Database** | PostgreSQL 16, Neo4j 5, Qdrant / ChromaDB |
| **AI / LLM** | OpenAI, OpenRouter, Ollama, LangGraph, CrewAI |
| **Auth** | JWT (python-jose), bcrypt |

---

## 📄 Лицензия | License

MIT License — see [LICENSE](LICENSE).

---

## 🤝 Как помочь | Contributing

Мы приветствуем вклад сообщества! См. [CONTRIBUTING.md](CONTRIBUTING.md).

Нам особенно нужна помощь с:
- Парсингом источников права РК
- Тестированием на реальных кейсах
- UI/UX дизайном
- Переводом на казахский язык

---

## 📬 Контакты | Contact

- Telegram: [@legal_ai_kz](https://t.me/legal_ai_kz) (скоро)
- GitHub Issues: [создать issue](https://github.com/your-org/legal-ai-agent/issues/new)

---

<div align="center">
  <sub>Built with ❤️ for the legal community of Kazakhstan</sub>
</div>
