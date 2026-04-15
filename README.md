<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12+-blue?style=flat-square&logo=python" />
  <img src="https://img.shields.io/badge/Ollama-local%20LLM-orange?style=flat-square" />
  <img src="https://img.shields.io/badge/LangGraph-pipeline-green?style=flat-square" />
  <img src="https://img.shields.io/badge/Lua-5.4%2F5.5-blueviolet?style=flat-square" />
  <img src="https://img.shields.io/badge/MTS-True%20Tech%20Hack-red?style=flat-square" />
</p>

<h1 align="center">LocalScriptLua</h1>

<p align="center"><b>Локальная AI-система для генерации, доработки, проверки и сохранения Lua-кода</b></p>

<p align="center">
Не просто чат с моделью — аккуратная рабочая среда для Lua.<br/>
Внешне мягкий интерфейс, внутри — серьёзная система для состояния, пайплайна и автопочинки кода.
</p>

---

## О проекте

**LocalScriptLua** — решение для кейса MTS True Tech Hack / LocalScript.

Система решает реальную инженерную задачу: LowCode Lua-скрипты часто описываются на естественном языке, а обычная LLM-генерация нестабильна по формату и логике. LocalScriptLua собирает задачу, состояние, код и проверки в единый рабочий поток — нужны не только код, но и проверка, сохранение и объяснение результата.

**Ключевые особенности:**

- Создаёт и дорабатывает Lua-код по описанию на русском или английском
- Проверяет код локально через Lua harness — без отправки кода наружу
- Делает fix-loop при ошибках — сам находит и исправляет проблемы
- Проверяет соответствие исходной задаче через LLM-verifier
- Отвечает на вопросы по текущему коду
- Сохраняет `.lua` и sidecar `jsonstring`-артефакт
- Работает полностью локально — без зависимости от внешних AI API

---

## Возможности системы

| Функция | Описание |
|---|---|
| **Генерация кода** | Создаёт Lua-скрипт по задаче на естественном языке |
| **Доработка кода** | Изменяет существующий скрипт по новому требованию |
| **Локальная валидация** | Запускает код в Lua harness с mock `wf.vars` / `wf.initVariables` |
| **Fix-loop** | Автоматически исправляет синтаксические и runtime-ошибки |
| **Проверка требований** | LLM-verifier сверяет логику кода с исходной задачей |
| **Semantic fix-loop** | Исправляет семантические ошибки после провала верификации |
| **Вопросы по коду** | Отвечает на вопросы об уже сгенерированном скрипте |
| **RAG-шаблоны** | Подбирает релевантный шаблон из базы 150+ Lua-примеров |
| **Сохранение** | `.lua` файл + sidecar `.jsonstring.txt` артефакт |
| **История чатов** | SQLite-хранилище с полной историей сессий |

---

## Архитектура

### Компоненты системы

```
Пользователь (Web UI / API / консоль)
         │
         ▼
      app.py  ──────────────────────────────┐
  (HTTP-сервер, чаты, состояние)            │
         │                                  │
         ▼                                  ▼
  PipelineEngine                    Файловая система
  (LangGraph-граф)                  (.lua, sidecar,
         │                           SQLite, логи, KB)
         ▼
      Ollama
  (LLM + embeddings)
         │
         ▼
  Lua interpreter
  (локальная валидация)
```

### Граф пайплайна

```
Start
  └─▶ TargetResolver        — определяет файл/папку активного чата
        └─▶ IntentRouter     — LLM решает: create / change / inspect / question
              ├─▶ QuestionAnswerer ──────────────────────────────────▶ End
              └─▶ PlannerAgent       — анализирует задачу, задаёт уточнения
                    └─▶ GenerationContextCompiler — компилирует контекст
                          └─▶ RAG    — подбирает Lua-шаблон из КБ
                                └─▶ CodeGenerator / CodeRefiner
                                      └─▶ CodeValidator
                                            ├─▶ [ошибка] ValidationFixer ──▶ CodeValidator
                                            └─▶ [ок] RequirementsVerifier
                                                  ├─▶ [ошибка] VerificationFixer ──▶ RequirementsVerifier
                                                  └─▶ [ок] ResponseAssembler ──▶ End
```

### Структура репозитория

```
TrueHack/
├── app.py                          # Web UI, HTTP API, chat runtime, console-режимы
├── requirements.txt                # Зависимости Python
├── Modelfile                       # Конфигурация Ollama-модели
├── openapi.yaml                    # Swagger-контракт API
├── docker-compose.yml              # Docker Compose (Ollama + App)
├── Dockerfile                      # Python 3.12 + Lua 5.4
├── .env.example                    # Шаблон переменных окружения
├── lua_rag_templates_kb.jsonl      # База шаблонов (150+ Lua-примеров)
├── src/
│   ├── agents/
│   │   └── planner.py              # TaskPlanner — анализ и уточнение задачи
│   ├── core/
│   │   ├── llm.py                  # LLM-провайдер (OpenAI-compatible Ollama)
│   │   ├── state.py                # PipelineState — хранилище данных пайплайна
│   │   └── logging_runtime.py     # Структурированное логирование
│   ├── graph/
│   │   ├── engine.py               # PipelineEngine — точка входа в граф
│   │   ├── builder.py              # Построение LangGraph-графа
│   │   ├── nodes.py                # Узлы пайплайна
│   │   └── conditions.py          # Условная маршрутизация
│   └── tools/
│       ├── lua_tools.py            # Lua harness, нормализация, диагностика
│       ├── target_tools.py         # Путь, именование, сохранение
│       ├── local_runtime.py        # Запуск Lua-процессов
│       └── rag_templates.py        # RAG: поиск и выбор шаблонов
├── docs/                           # Архитектурная документация
└── tests/                          # Тесты пайплайна, planner, RAG, логирования
```

---

## Быстрый старт (без Docker)

### Требования

- **Python** 3.12+
- **Ollama** — [скачать с ollama.com](https://ollama.com/download)
- **Lua 5.4 / 5.5** — интерпретатор, доступный в PATH или по явному пути

---

### Шаг 1 — Скачать репозиторий

```bash
git clone <url-репозитория>
cd TrueHack
```

---

### Шаг 2 — Создать виртуальное окружение и установить зависимости

**Windows (PowerShell):**
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

**Linux / macOS:**
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

### Шаг 3 — Установить Lua

**Windows:**
1. Скачать `lua-5.4.x_Win64_bin.zip` с [sourceforge.net/projects/luabinaries](https://sourceforge.net/projects/luabinaries/)
2. Распаковать, например в `C:\lua55\`
3. Добавить `C:\lua55\` в системную переменную `PATH`, **или** прописать полный путь в `.env`:
   ```
   LUA_BIN=C:/lua55/lua55.exe
   ```

**Ubuntu / Debian:**
```bash
sudo apt install lua5.4
```

**macOS:**
```bash
brew install lua
```

---

### Шаг 4 — Скачать модели через Ollama

Запустить Ollama (если ещё не запущен):
```bash
ollama serve
```

Скачать основную модель для генерации кода:
```bash
ollama pull qwen2.5-coder:7b-instruct
```

Скачать модель для планировщика, роутера и выбора шаблона:
```bash
ollama pull qwen2.5-coder:3b-instruct
```

Скачать эмбеддинг-модель для RAG (рекомендуется):
```bash
ollama pull qwen3-embedding:0.6b
```

> **Для GPU 8 GB VRAM** — используйте `qwen2.5-coder:7b-instruct` (рекомендуется).  
> **Для GPU 4 GB VRAM** — используйте `qwen2.5-coder:3b-instruct` для всех агентов.

**Создать оптимизированную модель с параметрами хакатона (рекомендуется):**
```bash
ollama create truehack -f Modelfile
```

---

### Шаг 5 — Настроить переменные окружения

Скопировать шаблон:
```bash
# Windows
copy .env.example .env

# Linux / macOS
cp .env.example .env
```

Открыть `.env` и указать путь к Lua, если он не в PATH:
```env
LUA_BIN=C:/lua55/lua55.exe
```

Остальные значения по умолчанию уже настроены для локального запуска.

---

### Шаг 6 — Запустить приложение

```bash
python app.py
```

Приложение откроется в браузере автоматически. Если нет — перейти по адресу:

```
http://127.0.0.1:8000
```

#### Дополнительные параметры запуска

```bash
# Указать рабочую папку для сохранения .lua файлов
python app.py --workspace C:\Work\LuaProjects

# Указать модель явно
python app.py --model qwen2.5-coder:3b-instruct

# Все параметры
python app.py \
  --host 127.0.0.1 \
  --port 8000 \
  --workspace /path/to/workspace \
  --model truehack \
  --url http://127.0.0.1:11434/v1
```

---

## Запуск через Docker Compose

Самый простой способ — всё поднимается одной командой, Ollama и приложение запускаются вместе.

### Требования

- [Docker Desktop](https://www.docker.com/products/docker-desktop/)
- NVIDIA Container Toolkit (для GPU-поддержки)

### Запуск

```bash
# Скопировать конфиг
copy .env.example .env    # Windows
cp .env.example .env      # Linux / macOS

# Поднять сервисы
docker compose up --build
```

Приложение будет доступно по адресу `http://127.0.0.1:8000`.

### Остановить

```bash
docker compose down
```

### Сервисы

| Сервис | Порт | Описание |
|---|---|---|
| `truehack-app` | `8000` | Web UI и REST API |
| `truehack-ollama` | `11434` | Ollama LLM-инференс |

---

## Конфигурация (.env)

Все параметры задаются через `.env` файл. Полный шаблон — `.env.example`.

### Основные параметры

```env
# Базовая модель (используется для агентов без явного override)
OLLAMA_MODEL=qwen2.5-coder:7b-instruct

# Адрес Ollama API
OLLAMA_BASE_URL=http://127.0.0.1:11434/v1

# Путь к исполняемому файлу Lua
LUA_BIN=lua55

# Ограничение параллельных запросов к Ollama (рекомендуется 1)
OLLAMA_MAX_CONCURRENT_REQUESTS=1
```

### Модели по агентам (per-agent routing)

Каждый агент пайплайна может использовать свою модель:

```env
OLLAMA_MODEL_CODE_GENERATOR=qwen2.5-coder:7b-instruct
OLLAMA_MODEL_CODE_REFINER=qwen2.5-coder:7b-instruct
OLLAMA_MODEL_VALIDATION_FIXER=qwen2.5-coder:7b-instruct
OLLAMA_MODEL_VERIFICATION_FIXER=qwen2.5-coder:7b-instruct
OLLAMA_MODEL_REQUIREMENTS_VERIFIER=qwen2.5-coder:7b-instruct
OLLAMA_MODEL_TEMPLATE_SELECTOR=qwen2.5-coder:3b-instruct
OLLAMA_MODEL_INTENT_ROUTER=qwen2.5-coder:3b-instruct
OLLAMA_MODEL_TASK_PLANNER=qwen2.5-coder:3b-instruct
```

### RAG-шаблоны

```env
RAG_TEMPLATES_ENABLED=true
RAG_TEMPLATES_KB_PATH=lua_rag_templates_kb.jsonl
RAG_TEMPLATES_TOP_K=5
RAG_TEMPLATES_EMBED_MODEL=qwen3-embedding:0.6b
```

### TaskPlanner

```env
PLANNER_ENABLED=true
```

---

## Использование

### Web UI

Основной способ работы. Открыть `http://127.0.0.1:8000`.

**Панель инструментов:**

| Кнопка | Действие |
|---|---|
| **Повторить Проверку** | Повторно запустить валидацию и верификацию текущего кода |
| **Статус** | Показать статус последнего выполнения пайплайна |
| **Путь** | Показать активный target-путь для сохранения |
| **Текущий Промпт** | Показать оригинальную задачу текущего чата |
| **Показать Код** | Отобразить последний сгенерированный код |
| **Помощь** | Справка по командам |

**Примеры задач:**

```
Напиши скрипт, который берёт массив items из wf.vars.json.DOC.ZCDF_PACKAGES
и возвращает только те элементы, у которых sku не пустой
```

```
Преобразуй структуру данных так, чтобы все элементы Items в ZCDF_PACKAGES
всегда были представлены в виде массивов, даже если они изначально не являются массивами
```

```
Добавь обработку случая когда wf.vars.json.DOC пустой
```

### Команды в чате

| Команда | Описание |
|---|---|
| `/new` | Начать новый чат |
| `/retry` | Повторить последнюю генерацию |
| `/code` | Показать текущий код |
| `/path [путь]` | Установить/показать путь сохранения |
| `/status` | Статус пайплайна |
| `/prompt` | Показать исходную задачу |

### REST API

Swagger UI доступен по адресу: `http://127.0.0.1:8000/swagger`

Основные эндпоинты:

```
POST /api/chat/{chat_id}/message   — отправить сообщение в чат
GET  /api/chats                    — список чатов
GET  /api/chat/{chat_id}           — история чата
POST /api/generate                 — one-shot генерация без истории
```

### Console-режим

Локальный диалог прямо в терминале:

```bash
python app.py --console
```

---

## Модели и параметры

### Рекомендованная конфигурация (GPU 8 GB VRAM)

| Агент | Модель |
|---|---|
| Генератор кода | `qwen2.5-coder:7b-instruct` |
| Исправление ошибок | `qwen2.5-coder:7b-instruct` |
| Верификатор требований | `qwen2.5-coder:7b-instruct` |
| Выбор шаблона | `qwen2.5-coder:3b-instruct` |
| Роутер интента | `qwen2.5-coder:3b-instruct` |
| Планировщик | `qwen2.5-coder:3b-instruct` |
| Эмбеддинги (RAG) | `qwen3-embedding:0.6b` |

### Параметры модели (Modelfile)

```
FROM qwen2.5-coder:7b-instruct

PARAMETER num_ctx     4096   # контекстное окно
PARAMETER num_predict 256    # максимум токенов в ответе
PARAMETER num_batch   1      # батч для VRAM-ограничений
PARAMETER num_gpu     99     # полная загрузка на GPU
PARAMETER temperature 0.2    # детерминированная генерация
```

---

## Артефакты и наблюдаемость

После каждой генерации система создаёт:

| Файл | Описание |
|---|---|
| `*.lua` | Готовый Lua-скрипт |
| `*.jsonstring.txt` | Sidecar-артефакт: JSON с `lua{...}lua`-обёрткой |

Логи и хранилище:

| Файл | Описание |
|---|---|
| `.lua_console_chats.db` | SQLite: чаты, сообщения, состояние сессий |
| `logs/runtime.jsonl` | Технические события пайплайна |
| `logs/llm_prompts.jsonl` | Все вызовы моделей (prompt-аудит) |

Автоочистка логов происходит каждые 10 запусков.

---

## Тесты

```bash
# Запустить все тесты
python -m pytest tests/ -v

# Тесты конкретного модуля
python -m pytest tests/test_planner.py -v
python -m pytest tests/test_rag.py -v
python -m pytest tests/test_logging.py -v
```

---

## Команда

**APPLExMISISxMIREA xXЯОМИ** — MTS True Tech Hack 2024

- Дёмин Владислав Русланович
- Ромашкин Дмитрий Олегович
- Гасанов Тимур Русланович

---

## Стек технологий

| Компонент | Технология |
|---|---|
| LLM-инференс | [Ollama](https://ollama.com) |
| Оркестрация пайплайна | [LangGraph](https://github.com/langchain-ai/langgraph) |
| LLM API-клиент | openai-python (OpenAI-compatible endpoint) |
| Логирование | [structlog](https://www.structlog.org/) |
| Хранилище чатов | SQLite |
| Валидация кода | Lua 5.4 (локальный harness) |
| RAG эмбеддинги | qwen3-embedding:0.6b (через Ollama) |
| Web UI | Встроенный HTTP-сервер (без внешних фреймворков) |
| Контейнеризация | Docker + Docker Compose |
