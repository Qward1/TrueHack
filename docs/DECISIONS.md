# Архитектурные решения

# Decisions log

## 2026-04-10
### Decision
Используем `app.py + src/graph` как единственный поддерживаемый runtime.

### Why
В проекте было дублирование orchestration между graph path и legacy runtime.

### Consequences
Вся новая логика встраивается только в canonical graph pipeline.

---

## 2026-04-10
### Decision
Сохраняем Lua target path logic как обязательную бизнес-функцию.

### Why
Система должна уметь писать Lua-файлы в нужный путь, создавать директории и переиспользовать active target.

### Consequences
`resolve_target` и `save_code` остаются core-частью runtime.

---

## 2026-04-10
### Decision
Исключаем generic README/text artifact orchestration из продуктового runtime.

### Why
Product scope — Lua-centric pipeline, а не универсальный редактор артефактов.

### Consequences
Runtime остается Lua-only, README — документация репозитория.

---

## 2026-04-10
### Decision
Generation/refine/fix/verify/explain используют единый LLM provider (`src/core/llm.py`).

### Why
Разрозненные direct client paths приводят к дублированию и нестабильному runtime.

### Consequences
Один abstraction layer для всех LLM-шагов pipeline.

---

## 2026-04-10
### Decision
Временно отключаем e2e gate в каноническом runtime.

### Why
Нужно упростить текущий рабочий цикл до `generate/refine -> validate -> verify -> save -> explain`,
сохранив пост-объяснение и предложения улучшений после записи файла.

### Consequences
`save_code` вызывается после успешной локальной валидации и проверки требований.
`src/tools/lua_tools.py` сохраняет e2e helpers, но graph path их сейчас не вызывает.

---

## 2026-04-10
### Decision
Временно отключаем `luacheck` в канонической локальной валидации.

### Why
Нужно, чтобы runtime проверял и чинил код через фактический запуск `lua`, без отдельного lint-шага.

### Consequences
`validate_code` теперь опирается на запуск через `lua`, а `luacheck` wrappers остаются в кодовой базе неиспользуемыми.
Ответ пользователю после сохранения по-прежнему формируется через `explain_solution` и содержит предложения улучшений.

---

## 2026-04-10
### Decision
Унифицируем naming и path sanitization для создаваемых папок, Lua-файлов и chat titles.

### Why
Сырые prompt/path fragments давали слабые имена (`uluchshi_kod`) и могли ломать сохранение на Windows из-за невалидных сегментов пути.

### Consequences
`src/tools/target_tools.py` теперь отвечает за единые naming helpers:
- более информативный slug для auto-created project artifacts;
- sanitization невалидных Windows path components;
- генерацию chat title из очищенного prompt.

---

## 2026-04-10
### Decision
После успешного сохранения система обязана возвращать объяснение решения и следующий шаг для пользователя.

### Why
Нужно не только выдать код, но и объяснить реализацию, предложить улучшения и задать уточняющие вопросы.

### Consequences
Добавлен `explain_solution` и хранение `suggested_changes`/`clarifying_questions` в chat state.
Follow-up вида `примени предложение N` поддерживается в следующем turn.

---

## 2026-04-10
### Decision
Текущий dev-runtime может использовать local OpenAI-compatible endpoint, но финальный hackathon target — Ollama.

### Why
Это прямое требование условий хакатона.

### Consequences
Docs должны явно различать текущее dev-state и финальный целевой runtime.
