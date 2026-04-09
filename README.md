# LocalScript

Локальная агентская система для генерации и валидации Lua-кода.
Работает полностью офлайн через LM Studio — никаких внешних API.

## Стек

| Компонент | Технология |
|-----------|-----------|
| Оркестрация агентов | LangGraph |
| LLM | Qwen2.5-Coder-3B-Instruct via LM Studio |
| API | FastAPI + Uvicorn |
| RAG | FAISS + sentence-transformers |
| БД | SQLite (aiosqlite) |
| Конфиг | YAML + Pydantic |
| Логирование | structlog |
| Lua | lua54 |

## Архитектура

```
src/
  core/       — конфиг, типы состояния, LLM-абстракция
  agents/     — router, planner, coder, validator, qa
  tools/      — lua_validator, lua_executor, rag
  graph/      — LangGraph граф и роутинг
  storage/    — SQLite, история чатов
  api/        — FastAPI эндпоинты
  ui/         — веб-интерфейс (HTML + JS)
config/
  settings.yaml      — основная конфигурация
  prompts/*.txt      — промпты агентов
data/
  lua_docs/          — документация Lua для RAG
  localscript.db     — база данных (создаётся автоматически)
  rag_index/         — FAISS-индекс (создаётся при первом запуске)
```

## Быстрый старт (Windows)

### 1. Требования

- Python 3.11+
- [LM Studio](https://lmstudio.ai/) с загруженной моделью `Qwen2.5-Coder-3B-Instruct`
- `lua54` доступен в PATH

### 2. Установка

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -e .[dev]
```

### 3. Запуск LM Studio

Откройте LM Studio, загрузите модель `Qwen2.5-Coder-3B-Instruct`
и запустите локальный сервер на `http://localhost:1234`.

### 4. Запуск API-сервера

```bat
.venv\Scripts\activate
uvicorn src.api.app:app --reload --port 8000
```

Веб-интерфейс будет доступен по адресу `http://localhost:8000`.

### 5. Тесты

```bat
pytest
```

## Конфигурация

Все настройки в [config/settings.yaml](config/settings.yaml).
Параметры генерации для каждого агента задаются отдельно (`temperature`, `max_tokens`).
