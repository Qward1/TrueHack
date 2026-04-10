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
Generation/refine/fix/verify/e2e-suite используют единый LLM provider (`src/core/llm.py`).

### Why
Разрозненные direct client paths приводят к дублированию и нестабильному runtime.

### Consequences
Один abstraction layer для всех LLM-шагов pipeline.

---

## 2026-04-10
### Decision
Перед сохранением обязателен отдельный e2e gate:
`generate_e2e_suite -> run_e2e_suite -> save_code`.

### Why
По бизнес-требованию система должна делать auto-generated end-to-end проверку до записи финального файла.

### Consequences
`save_code` вызывается только при успешном e2e.
При провале e2e pipeline возвращается в fix-loop (если еще не исчерпан лимит итераций).

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
