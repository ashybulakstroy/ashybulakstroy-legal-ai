# Прогресс

## Исправления

### article ranking (legislation_service.py)
- `find_relevant_articles()`: скор via `title_score + content_matches*3`, `token_coverage_bonus`, лимит 20→25

### LLM prompt (ai_service.py)
- `_format_articles_for_prompt()`: 200→500 символов на статью, 2→3 статей на закон, 10→15 законов

### LLM failover + авто- discovery (ai_service.py)
- `_FALLBACK_MODELS = ["llama-3.1-8b-instant", "llama-3.3-70b-versatile"]`
- `_discover_models()` — получает все модели с прокси, фильтрует только чат-модели, приоритезирует известные рабочие, кеширует глобально
- Порядок: hardcoded (без auto) → hardcoded (с auto → 404 триггерит discovery) → discovery (без auto)
- Никакого `asyncio.wait_for()` — LLM вызовы идут без обёртки
- `_is_unhelpful()` — проверка на пустой/бесполезный ответ внутри `_try_openai_model()`

### Очистка от дебага
- `.env`: `OPENAI_TIMEOUT=25` → `60`
- `main.py`: удалён `/debug` endpoint
- `routes.py`: удалены timing-логи (`import time`, `logger.warning("TIMING...")`)

### Фильтр цитирования статей (routes.py)
- Был: искал номера статей только в секции `**Нормы:**`, regex не ловил множественные ("ст. 1, 2, 3"), требовал ≥2 слов из названия закона в тексте анализа
- Стал: поиск по всему тексту, regex ловит "стать[яиюеях]" и множественные номера, нет проверки названия закона, есть fallback по базовому номеру ("14" подходит для "14-1")

### Скрипты
- `run_server.ps1`, `start_server.ps1`: исправлен путь на `.venv/Scripts/python.exe`, чистится PATH от Prj_21_Odoo

## Проверенные рабочие модели
- `llama-3.1-8b-instant` — самая быстрая (~1с)
- `qwen/qwen3-32b` (~2с)
- `models/gemini-2.5-flash` (~7с)
- `meta-llama/llama-4-scout-17b-16e-instruct`

## Остаётся
- **Скорость**: primary модель `openai/gpt-oss-20b` даёт ~30-40с ответ. Fallback `llama-3.1-8b-instant` работает за 1-5с. Можно сменить `LLM_MODEL` в `.env`
- **Healthcheck** перед discovery (опционально)
