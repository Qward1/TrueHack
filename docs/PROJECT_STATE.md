# Текущее состояние

# Project state

## Current status
Репозиторий работает в одном каноническом runtime (`app.py + src/graph`) и поддерживает полный цикл:

`generate/refine -> local validate -> fix -> requirement verify -> save -> explain/respond`

Если в чате есть явный target path или уже выбран active target, сохранение итогового кода происходит после успешной локальной валидации и проверки требований:
- canonical artifact: чистый `.lua` файл;
- дополнительный artifact: sidecar `*.jsonstring.txt` с `lua{...}lua`.
Если path не указан и active target отсутствует, код проходит pipeline и возвращается в чат без записи на диск.

## Что работает
- единый LangGraph pipeline без дублирующего orchestration path
- path-aware Lua target logic:
  - explicit `.lua` path;
  - директория -> slug-папка + slug.lua;
  - active target в рамках чата
  - отсутствие неявного fallback-save для нового чата без пути
  - санитизация невалидных Windows-сегментов пути перед созданием файлов и папок
- dual-save результата:
  - чистый `.lua` файл для runtime;
  - JsonString sidecar рядом с ним для LowCode contract
- генерация Lua-кода
- refine существующего Lua-кода
- generation contract для LowCode:
  - `Lua 5.5`
  - wrapper `lua{ ... }lua`
  - workflow/LUS script instead of console/CLI app
  - direct access вместо JsonPath
  - `wf.vars` / `wf.initVariables` для данных схемы
- локальная валидация через `lua` с temporary LowCode harness
- nested mock paths для `wf.vars` / `wf.initVariables` в validation harness
- fix loop по стадиям ошибок
- normalized runtime repair hints для common Lua errors (`bad argument`, `nil` access/call, arithmetic/type mismatch, concatenation)
- LLM verification требований
- deterministic guard для object-key cleanup задач:
  - plain `return wf.vars...` больше не считается корректным результатом;
  - перед save требуется явная трансформация/очистка запрошенных ключей
- route guard для `change` without code:
  - если existing code отсутствует, pipeline не уходит в `refine_code` и не пишет warning fallback;
  - вместо этого запрос обрабатывается как generate-path с сохранением intent
- bare field resolution по parseable context:
  - уникальное имя поля из prompt может быть автоматически разрешено в `wf.vars.*` / `wf.initVariables.*`;
  - verification использует это как обязательный expected path even when the user did not write the full workflow path
- улучшенный naming:
  - более информативный slug для auto-created project folder / Lua file;
  - очищенный title чата без сырого пути из prompt
- post-save объяснение решения:
  - что в коде;
  - как работает;
  - предложения улучшений;
  - уточняющие вопросы
- follow-up вида `примени предложение N` в следующем цикле
- команды `/path`, `/status`, `/code`, `/prompt`, `/new`, `/edit`, `/retry`

## Что удалено/не используется
- второй runtime (`main.py`)
- legacy standalone editor path
- generic README/text artifact orchestration в продуктовой логике
- direct legacy LLM client path

## Что еще не закрыто
- фиксация финального demo flow под жюри
- подтверждение VRAM-лимита `<= 8 GB` на целевом конфиге
- возврат e2e-gate после отдельного решения по нему
- автоматизированные regression tests (пока только smoke/manual checks)
- REST API `/generate` endpoint по OpenAPI spec

## Runtime status
- Ollama как canonical LLM runtime
- default `base_url`: `http://127.0.0.1:11434/v1`
- default model: `qwen2.5-coder:7b-instruct`
- параметры хакатона зафиксированы в `Modelfile` (num_ctx=4096, num_predict=256, num_gpu=99)
- смена модели через `--model`, `OLLAMA_MODEL` или кастомный Modelfile

## Demo reproducibility
README описывает канонический запуск через `app.py` и текущий pipeline.
Для воспроизведения нужны:
- Python deps из `requirements.txt`
- `lua`
- Ollama + модель `qwen2.5-coder:7b-instruct`

## Open risks
- эвристики path resolution пока не покрыты автоматическими тестами
- при недоступности LLM часть шагов (generate/verify/explain) недоступна
- без e2e-gate сохранение теперь опирается только на локальную валидацию и LLM verification
- без `luacheck` lint-класс проблем сейчас не отлавливается отдельным шагом
- naming эвристики остаются rule-based и могут потребовать дальнейшей подстройки под реальные prompt patterns
- mock values в validation harness остаются эвристическими и не заменяют реальные platform variables

## Next tasks
- добавить автотесты для:
  - target resolution;
  - save gate без e2e;
  - follow-up применения предложений

---

## 2026-04-11 update: LowCode generation alignment
- The pipeline now assembles prompts around task/context splitting instead of sending raw user text as-is.
- Public-sample few-shot patterns are embedded locally in `src/graph/nodes.py` and steer output toward short workflow chunks instead of demo applications.
- Requirement verification now has a deterministic guard:
  - compare expected `wf.vars.*` / `wf.initVariables.*` paths from the prompt with actual workflow paths in code;
  - reject invented demo tables like `local data = {...}` / `local emails = {...}` when workflow context was provided;
  - require direct `return` for simple workflow extraction/computation tasks unless the prompt explicitly asks to save into `wf.vars`.
- Save is now blocked on deterministic workflow-contract failures, including the case where semantic LLM verification is unavailable.
- Automated regression coverage exists in `tests/` via stdlib `unittest`:
  - prompt/context splitting;
  - deterministic LowCode alignment guard;
  - pipeline scenario: app-style generation goes into fix-loop;
  - pipeline scenario: public-sample-style generation passes validate -> verify -> save.

## 2026-04-11 update: hybrid compiler for workflow data tasks
- The canonical pipeline now inserts `prepare_generation_context` between intent routing and code generation/refinement.
- Parsed workflow JSON is normalized into a compiled request object and used as the main source of truth for:
  - path inventory and types;
  - operation detection;
  - primary-path selection;
  - clarification gating on ambiguity;
  - deterministic fast-path generation for simple data tasks.
- Current deterministic simple scope:
  - count array;
  - first/last element from array;
  - direct field/scalar extraction;
  - increment/decrement numeric scalar;
  - string length.
- Example target behavior now covered by tests:
  - `Посчитай количество товаров в корзине` + workflow context -> `return #wf.vars.cart.items`
- Ambiguous requests with multiple matching paths now return a clarification response instead of guessing and instead of saving invalid code.
