# Текущее состояние

# Project state

## Current status
Репозиторий работает в одном каноническом runtime (`app.py + src/graph`) и поддерживает полный цикл:

`generate/refine -> local validate -> fix -> requirement verify -> save -> explain/respond`

Если в чате есть явный target path или уже выбран active target, сохранение итогового кода происходит после успешной локальной валидации и проверки требований:
- canonical artifact: чистый `.lua` файл;
- дополнительный artifact: sidecar `*.jsonstring.txt` как JSON object, где value содержит `lua{...}lua`.
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
  - JSON sidecar рядом с ним для LowCode contract, где code value хранится как `lua{...}lua`
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
- stricter internal fix retry:
  - если первый fix-кандидат пустой, почти идентичен прошлому коду или повторяет те же deterministic requirement failures, pipeline делает ещё один более жёсткий fix-call до возврата в validation
- LLM verification требований
- conflict-safe verification merge:
  - deterministic findings are now passed into the LLM verifier as hard constraints;
  - if LLM still returns an optimistic pass against deterministic failures, the final verdict stays failed and records a verifier-conflict warning instead of showing a misleading positive summary
- model-driven Lua synthesis with compiler-assisted context:
  - prompts no longer rely on embedded code templates or shortest-return bias;
  - compiler still supplies path inventory, clarification gating, and verification hints, but prompt payloads now only keep the task, selected workflow path, current context, and short mandatory-fix lists;
  - generation can legitimately produce longer multi-step workflow scripts when the task needs normalization, iteration, guards, or helper functions;
  - generation/refine/fix now run with a lower temperature for parseable workflow context;
  - raw model output is now treated as invalid unless it starts with `lua{` and ends with `}lua`, without surrounding quotes or code fences;
  - `fix_code` no longer includes the previous broken assistant answer in the repair prompt, to reduce pattern-copying loops
- response normalization for generation/fix:
  - runtime extracts Lua both from plain `lua{...}lua` and from structured JSON envelopes with code-bearing fields;
  - extraction also covers fenced/meta-wrapped JSON payloads, so validation does not execute literal escape-text like `\\n ...`;
  - this blocks a class of false validation failures where the model returns metadata + code instead of raw Lua body
- user-facing/export formatting:
  - чат и sidecar now emit JSON payloads with a named field whose value is the wrapped `lua{...}lua` string;
  - field name is chosen from selected save path, selected primary workflow path, or target stem
- deterministic guard для object-key cleanup задач:
  - plain `return wf.vars...` больше не считается корректным результатом;
  - перед save требуется явная трансформация/очистка запрошенных ключей
- deterministic semantic guard для shape-sensitive tasks:
  - array normalization больше не проходит, если код проверяет только `type(x) == "table"` и не различает object vs array semantics;
  - array means table with numeric keys `1..n` without gaps; empty table is treated as an array;
  - `next(x)` / empty-vs-non-empty checks не считаются достаточным различением object vs array semantics;
  - простая перемаркировка исходного workflow object/scalar через `_utils.array.markAsArray(source)` блокируется;
  - misuse of `_utils.array.new(...)` with inline arguments blocks save;
  - создание нового массива без `_utils.array.markAsArray(arr)` blocks save
- route guard для `change` without code:
  - если existing code отсутствует, pipeline не уходит в `refine_code` и не пишет warning fallback;
  - вместо этого запрос обрабатывается как generate-path с сохранением intent
- hybrid intent routing:
  - первичное решение об intent принимается по фактическому наличию кода, clarification state и текстовым сигналам;
  - LLM intent classifier теперь используется как fallback для неоднозначных кейсов, а не как единственный источник решения;
  - запросы с change-like формулировкой, но без existing code и без pasted Lua, переопределяются в `create`;
  - pasted Lua в сообщении может быть использован как источник existing code для legit `change` / `inspect`
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
- полное automated regression покрытие для compiler/pipeline flows
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
- если модель вернёт completely non-code JSON без `lua` / `code` / `script`, normalizer всё равно не сможет угадать intent и validation rightly fail
- plausible-but-shallow code now чаще блокируется verify/save gate, даже если workflow path выбран правильно

## Next tasks
- добавить автотесты для:
  - target resolution;
  - save gate без e2e;
  - follow-up применения предложений

---

## 2026-04-11 update: LowCode generation alignment
- The pipeline now assembles prompts around task/context splitting instead of sending raw user text as-is.
- Prompt steering is now abstract and model-driven: it describes workflow-script constraints and synthesis strategy without embedding concrete code templates from sample tasks.
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
  - clarification gating on ambiguity.
- The compiler no longer bypasses the main LLM with deterministic script templates for simple tasks.
- Instead it anchors model generation and verification so the same pipeline can cover both short extractions and longer multi-step workflow scripts.
- Ambiguous requests with multiple matching paths now return a clarification response instead of guessing and instead of saving invalid code.
