# План миграции: law-kz-mcp → ashybulakstroy/ashybulakstroy-legal-ai

## Цель
Перенести рабочий проект law-kz-mcp в репозиторий ashybulakstroy-legal-ai как временную точку входа (`dev/stage_1/`). Позже, при разработке основного функционала внешнего проекта, плавно перейти на его нативную структуру.

---

## Фаза 1: Подготовка локальной файловой системы

### 1.1 Очистка law-kz-mcp перед копированием
Удалить машинно-зависимые файлы (не попадут в git):

```
из корня law-kz-mcp удалить:
  .venv/                  # виртуальное окружение
  __pycache__/            # кеш Python (во всех вложенных папках)
  law_kz.db               # база данных SQLite
  *.pyc                   # скомпилированные файлы
  .env                    # секреты (оставить .env.example)
  last_resp.json
  last_resp2.json
  resp.json
  fix_summary.py
```

### 1.2 Создать `.gitignore` для stage_1
Файл `dev/stage_1/.gitignore`:

```
.venv/
__pycache__/
*.pyc
*.pyo
.env
law_kz.db
*.db
last_resp*.json
resp.json
.DS_Store
```

### 1.3 Скопировать проект в dev/stage_1 (на локальной ФС перед git pull)
```bash
# Из корня ashybulakstroy-legal-ai (локальная копия):
mkdir -p dev/stage_1

# Копируем всё из law-kz-mcp, исключая мусор
robocopy C:\Work\Prj_24_LAW_KZ dev\stage_1 /E /XD .venv __pycache__ .git /XF law_kz.db *.pyc .env last_resp.json last_resp2.json resp.json fix_summary.py
```

---

## Фаза 2: Слияние с GitHub-репозиторием

### 2.1 Клонировать внешний репозиторий (если ещё не склонирован)
```bash
cd C:\Work
git clone https://github.com/ashybulakstroy/ashybulakstroy-legal-ai.git
cd ashybulakstroy-legal-ai
```

### 2.2 Разместить `dev/stage_1/` внутри клонированного репо
Скопировать подготовленную папку `dev/stage_1/` в корень клонированного репо:

```bash
# Из временной папки, где лежит подготовленный stage_1:
xcopy /E /I "C:\Temp\prepared\dev\stage_1" "C:\Work\ashybulakstroy-legal-ai\dev\stage_1"
```

Либо, если stage_1 уже лежит рядом:
```bash
move C:\Work\Prj_24_LAW_KZ\dev\stage_1 C:\Work\ashybulakstroy-legal-ai\dev\stage_1
```

### 2.3 Закоммитить и запушить
```bash
cd C:\Work\ashybulakstroy-legal-ai
git add dev/stage_1/
git commit -m "feat: add working baseline (dev/stage_1) from law-kz-mcp"
git push
```

---

## Фаза 3: Локальный запуск stage_1 (после pull)

### 3.1 Склонировать репо на целевой ПК (или использовать текущий)
```bash
git clone https://github.com/ashybulakstroy/ashybulakstroy-legal-ai.git
cd ashybulakstroy-legal-ai
```

### 3.2 Настроить и запустить stage_1
```powershell
cd dev\stage_1

# Создать .env из шаблона (скопировать .env.example → .env и вписать ключи)
copy .env.example .env

# Создать виртуальное окружение и установить зависимости
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Загрузить данные (создаст law_kz.db с ~34 законами, ~7400 статей)
python seed.py

# Запустить сервер (фоново)
.\run_server.ps1 start

# Проверить:
#   http://localhost:8000          — веб-интерфейс
#   http://localhost:8000/docs     — Swagger
#   http://localhost:8000/health   — healthcheck
```

---

## Фаза 4: Будущий переход на нативную структуру

### 4.1 Когда начинать
Когда в `backend/app/` внешнего проекта появится минимально рабочий функционал.

### 4.2 Как переносить
| Из `dev/stage_1/app/` | В `backend/app/` |
|----------------------|------------------|
| `models/legislation.py` | `models/legislation.py` |
| `schemas/legislation.py` | `schemas/legislation.py` |
| `services/legislation_service.py` | `services/legislation_service.py` |
| `services/ai_service.py` | `services/ai_service.py` |
| `api/routes.py` | `api/routes.py` |
| `parsers/` | `ingestion/` (адаптировать) |
| `database.py`, `config.py`, `main.py` | `db/`, `core/`, `main.py` (адаптировать) |
| `mcp_server.py`, `mcp_sse.py`, `mcp_cli.py` | `mcp/` |

### 4.3 Переключение точек входа
1. Остановить `dev/stage_1` (`.\run_server.ps1 stop`)
2. Запустить нативный бэкенд: `cd backend && uvicorn app.main:app --port 8000`
3. Фронтенд внешнего проекта (`frontend/`) перенаправить на нативный бэкенд
4. `dev/stage_1/` оставить как reference, позже удалить

---

## Структура после миграции (в репо ashybulakstroy-legal-ai)

```
ashybulakstroy-legal-ai/
├── .github/
├── backend/                  # скелет для будущей разработки
├── frontend/                 # React-фронтенд внешнего проекта
├── dev/
│   └── stage_1/               # РАБОЧАЯ ТОЧКА ВХОДА (сейчас)
│       ├── app/              # law-kz-mcp (полностью рабочий бэкенд)
│       │   ├── main.py
│       │   ├── api/routes.py
│       │   ├── models/
│       │   ├── services/
│       │   ├── parsers/
│       │   └── templates/
│       ├── scripts/
│       ├── seed.py
│       ├── requirements.txt
│       ├── run_server.ps1
│       └── .gitignore
├── .env.example
├── README.md
└── ...
```

---

## Ключевые моменты
- **База данных** остаётся SQLite (`dev/stage_1/law_kz.db`) — не меняем.
- **LLM-провайдер** настраивается через `.env` (Ollama / OpenAI-прокси).
- **Все API работают** сразу после запуска: поиск, юр. консультация, MCP, стриминг-прогресс.
- **Фронтенд** внешнего проекта (`frontend/`) пока не привязан — запускается отдельно, после настройки указывает на `localhost:8000/api/v1/...`.
- **Переход на нативную структуру** — инкрементальный, без простоев.
