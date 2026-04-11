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
Переводим generation contract на LowCode-формат `Lua 5.5 + lua{...}lua`.

### Why
Новые продуктовые условия требуют описывать скрипт как JsonString, хранить данные схемы через `wf.vars` / `wf.initVariables` и не использовать JsonPath.

### Consequences
Prompt contract и user-facing code representation обновлены под `lua{...}lua`, при этом runtime продолжает валидировать и сохранять чистое Lua-body после нормализации wrapper.

---

## 2026-04-10
### Decision
Сохраняем оба представления результата: canonical `.lua` и JsonString sidecar.

### Why
Продуктовый контракт требует работать и с исполняемым Lua-файлом, и с форматом `lua{...}lua`, но дублировать pipeline ради этого не нужно.

### Consequences
`save_code` выполняет один save-step и пишет:
- основной `.lua` файл для runtime;
- соседний `*.jsonstring.txt` c wrapper `lua{...}lua`.
UI `/status` и финальный ответ показывают оба пути, если сохранение прошло успешно.

---

## 2026-04-10
### Decision
Локальная validation для LowCode-скриптов запускается через temporary mock harness.

### Why
Голый запуск `lua script.lua` давал ложные падения на `wf.initVariables` и `_utils`, хотя проблема была в отсутствии платформенного контекста, а не в самом Lua-коде.

### Consequences
`validate_code` теперь исполняет временный harness, который:
- создаёт mock `wf.vars` и `wf.initVariables`;
- добавляет `_utils.array.new()` / `_utils.array.markAsArray(arr)`;
- строит nested mock tables для найденных `wf.vars.*` / `wf.initVariables.*`, в том числе при alias access.
Это снижает ложные validation failures и даёт fix-loop реальные runtime diagnostics.

---

## 2026-04-11
### Decision
Канонический runtime генерирует workflow/LUS scripts, а не console/CLI Lua-программы.

### Why
Текущий продуктовый формат — это data/workflow chunk с работой через `wf.vars` / `wf.initVariables`, а не интерактивное консольное приложение.

### Consequences
Generation/refine/fix prompts теперь требуют:
- workflow/LUS script shape;
- прямую работу с `wf.vars` и `wf.initVariables`;
- возврат значения и/или обновление `wf.vars`;
- отсутствие console input/output по умолчанию.
Локальная validation дополнительно блокирует `io.read` / `io.stdin:read` как нарушение активного контракта.

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

## 2026-04-11
### Decision
Миграция canonical runtime с LM Studio на Ollama завершена.

### Why
Прямое требование условий хакатона: модель должна запускаться через Ollama.

### Consequences
- Дефолт: `http://127.0.0.1:11434/v1` + `qwen2.5-coder:7b-instruct`
- Параметры хакатона зафиксированы в `Modelfile` (num_ctx=4096, num_predict=256, num_gpu=99)
- Смена модели: CLI `--model`, env `OLLAMA_MODEL`, или кастомный Modelfile
- Для 4GB VRAM: `--model qwen2.5-coder:3b-instruct`
- LM Studio env-переменные (`LMSTUDIO_MODEL`, `LMSTUDIO_URL`) удалены
- AsyncOpenAI клиент не изменён — Ollama совместим с OpenAI API

---

## 2026-04-11
### Decision
Workflow-path alignment is now enforced by a deterministic guard before save.

### Why
LLM-only verification was insufficient for public-sample tasks: the model could still return an app-style script with invented input tables and pass semantic scoring. The product requirement is stricter: generated Lua must operate on the provided workflow structure directly.

### Consequences
- `verify_requirements` now merges semantic LLM verification with deterministic checks.
- Deterministic checks validate:
  - direct usage of expected `wf.vars.*` / `wf.initVariables.*` paths when they are explicitly mentioned;
  - absence of invented demo tables and app/service wrappers when workflow context is present;
  - direct `return` for simple extraction/computation tasks unless the request explicitly asks to save into `wf.vars`.
- `check_verification` no longer allows save when deterministic `missing_requirements` are present, even if the semantic verifier returns a high score or is temporarily unavailable.

---

## 2026-04-11
### Decision
Не создаём fallback file target для нового чата без явного пути.

### Why
Пользовательское ожидание разделено на два режима:
- без явного path код нужен только как ответ в чате;
- с явным path система должна работать как file-based Lua builder.
Автоматическое сохранение в workspace без явного запроса смешивало эти режимы и создавало лишние файлы.

### Consequences
- `resolve_target` по-прежнему умеет:
  - explicit `.lua` path;
  - директорию;
  - active target текущего чата.
- Если новый turn не содержит path и active target ещё не задан, pipeline всё равно выполняет `validate -> verify -> explain/respond`, но `save_code` намеренно пропускает запись на диск.
- Пользователь получает код в чате без save-error.

---

## 2026-04-11
### Decision
Cleanup/remove-key задачи в workflow object data не считаются простыми `return`-операциями.

### Why
Фраза вида `очисти/удали ключи ...` может сослаться на правильный workflow path, но всё равно требовать реальную трансформацию массива/объекта. Старый deterministic guard пропускал `return wf.vars.some.path`, если путь был выбран верно.

### Consequences
- Operation detection выделяет key-cleanup запросы отдельно от простого `return`.
- Для таких задач простой `return` исходного workflow path блокирует save.
- Verification требует явного упоминания и обработки запрошенных ключей перед возвратом результата.

---

## 2026-04-11
### Decision
Intent `change` без existing code не должен заходить в refine-path.

### Why
LLM intent classifier может выбрать `change` для новых задач со словами `улучши`, `исправь`, `очисти`, даже если в чате или target file ещё нет текущего кода. В этом случае вход в `refine_code` создавал ложный warning и тут же падал обратно в `generate_code`.

### Consequences
- routing после preparation теперь учитывает не только intent, но и наличие `current_code`;
- `change`/`retry` без existing code идут сразу в `generate_code`;
- warning `no existing code — falling back to generate_code` остаётся только как defensive fallback, а не как штатный путь.

---

## 2026-04-11
### Decision
Bare field names from the task can be resolved to workflow paths using the pasted context.

### Why
Пользователь часто пишет `recallTime`, `emails`, `DATUM`, `TIME` без полного `wf.vars.*` / `wf.initVariables.*`. Если parseable workflow context уже есть в сообщении, отсутствие такого разрешения делает deterministic verification слишком слабой и позволяет сохранить код с неправильным workflow path.

### Consequences
- compiler добавляет inferred explicit paths, если bare field name однозначно соответствует одному workflow path в pasted context;
- verification использует эти inferred paths как expected workflow paths;
- это остаётся общим правилом по parseable context, а не special-case под конкретный prompt.

---

## 2026-04-11
### Decision
Fix-loop should receive normalized repair hints for common Lua runtime errors.

### Why
Raw stderr alone is often too noisy. For recurring Lua failures such as `bad argument`, `attempt to index/call nil`, arithmetic/type mismatch, or concatenation errors, the system needs a more stable signal about the root cause.

### Consequences
- validation diagnostics now include generic repair hints extracted from runtime errors;
- `fix_code` prompt includes these hints together with raw stderr;
- this remains API-agnostic and is not hardcoded to one function like `os.time`.

---

## 2026-04-11
### Decision
Simple workflow data tasks are compiled deterministically from parsed context before main LLM generation.

### Why
Prompt-only steering was insufficient. The model could still fall back to tutorial/application-style code whenever the request drifted away from the few-shot examples. The missing piece was structural understanding of pasted workflow JSON.

### Consequences
- The pipeline now compiles parseable workflow context into an internal request object before generation/refinement.
- For simple single-target tasks, code is generated deterministically instead of asking the main LLM.
- When multiple workflow paths match the request with similar confidence, the system asks for clarification and stops before generation/save.
- Main LLM generation remains for complex transformations, but now receives normalized workflow-path/type context rather than only raw pasted JSON.
