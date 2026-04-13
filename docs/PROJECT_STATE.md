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
- TaskPlanner LLM-агент (`plan_request` node) перед `prepare_generation_context`:
  - переформулирует задачу, идентифицирует workflow paths и target operation;
  - при ambiguity задаёт 1-3 уточняющих вопроса и возвращает clarification response;
  - на следующем turn ответ пользователя идёт **напрямую** в планировщик через `route_from_start` bypass — без захода в `resolve_target`/`route_intent`;
  - после 2 попыток уточнения принудительно продолжает pipeline;
  - planner result сохраняется в state, но generation/refine/fix prompts больше не дублируют его отдельной секцией, если те же данные уже выражены через `Task` и `Workflow anchor`;
  - отключается через `PLANNER_ENABLED=false`
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
- `CodeValidator` больше не должен ограничиваться пересказом traceback: hint-prompt требует назвать root cause, объяснить, почему код падает именно на данном validation context, и дать exact repair path
- fix loop по стадиям ошибок
- validation-fix prompt is now runtime-focused and no longer includes task/planner/workflow-context sections from `compiled_request`
- normalized runtime repair hints для common Lua errors (`bad argument`, `nil` access/call, arithmetic/type mismatch, concatenation)
- stricter internal fix retry:
  - если первый fix-кандидат пустой, почти идентичен прошлому коду или повторяет те же semantic requirement failures, pipeline делает ещё один более жёсткий fix-call до возврата в validation
- LLM verification требований
- workflow-state-aware semantic verification:
- verifier sees parsed workflow context and the before/after workflow snapshot captured during validation, without отдельного planner-summary дубля;
- если parsed workflow context и original workflow state совпадают, verifier prompt не дублирует оба полных snapshot-а;
  - explicit type/shape hints from compiled workflow context (`selected_primary_type`, `requested_item_keys`, `semantic_expectations`) are treated as mandatory verifier/fixer constraints;
  - verifier returns `passed`, `summary`, `missing_requirements`, and `warnings`, without any numeric score field or checklist object;
  - if the first verifier pass returns an optimistic false positive while the workflow-state evidence contradicts the request, a second contradiction-focused verifier pass can overrule it
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
- verifier/fix loop for cleanup and shape-sensitive tasks:
  - save is blocked by semantic `missing_requirements` and failed verifier verdicts;
  - fix prompts now carry concrete logic failures, including contradictions between the request and the observed workflow-state changes
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
- Requirement verification now relies on semantic LLM review plus concrete runtime evidence from validation.
- Save is blocked when semantic verification returns `passed=false` or non-empty `missing_requirements`.
- Automated regression coverage exists in `tests/` via stdlib `unittest`:
  - prompt/context splitting;
  - runtime-result-aware logic verification;
  - pipeline scenario: app-style generation goes into fix-loop;
  - pipeline scenario: workflow-style generation passes validate -> verify -> save.

## 2026-04-12 update: TaskPlanner integrated into canonical pipeline
- New LangGraph node `plan_request` powered by `src/agents/planner.py` runs between intent routing and `prepare_generation_context`.
- Toggle: env `PLANNER_ENABLED` (default `true` in `.env.example`); when off, the node short-circuits with `planner_skipped=True` and the rest of the pipeline runs as before.
- Clarification follow-up bypass: when the planner asks a question, state carries `awaiting_planner_clarification`, `planner_pending_questions`, `planner_original_input`, `planner_clarification_attempts` across turns. On the next turn the new `route_from_start` conditional edge routes directly to `plan_request`, skipping `resolve_target` and `route_intent`. The planner sees a merged input combining the original task, the questions it asked, and the user's answer.
- Safety: after `MAX_CLARIFICATION_ATTEMPTS` (=2) the planner forces continue to avoid infinite loops.
- Prompt enrichment from planner is no longer duplicated into generation/refine/fix builders when the same task/path signal is already present through `task_text` and `Workflow anchor`. Deterministic compiler remains the source of truth for path-level decisions.
- Soft re-compilation: if the request had no parseable context but the planner's `reformulated_task` lets the deterministic compiler discover more `expected_workflow_paths`, the enriched compiled_request is preferred.
- Persistence: `app.py` stores the four planner state fields per chat in `_empty_state_dict` / `_normalize_state_dict`, threads them into `process_message`, copies the result back via `_apply_pipeline_result`, and resets them on `/new`.
- Tests: 8 new e2e tests in `tests/test_planner_integration.py`; full suite (`python -m unittest discover tests -v`) passes 92 tests.

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

## 2026-04-12 update: format and logic verification hardening
- Workflow-context parsing now also repairs loose pasted fragments like `{ } "wf": {...}` into a valid JSON object before compilation and validation.
- Lua normalization now recovers common malformed wrapper variants such as fenced ```` ```lua{...}lua ``` ```` and trailing `}lua` remnants, so wrapper noise does not turn into a false syntax failure by itself.
- Runtime marker extraction now works for both LF and CRLF output produced by the temporary Lua harness.
- The local validation harness now captures both the actual returned Lua value and the updated workflow snapshot on the provided workflow context.
- Semantic verification now sees parsed workflow context and the updated workflow state, so logic review can fail on wrong mutations instead of only reviewing source text.
- `explain_solution` now accepts explainer section fields as either JSON arrays or plain strings before generic fallback text is used.
- If validation or requirement verification still fails after the fix loop, the user-facing response remains diagnostic, still shows the current code payload, and does not save artifacts to disk.

## 2026-04-12 update: per-agent Ollama models
- The canonical runtime still uses one `LLMProvider`, but model selection is now resolved per LLM agent at call time.
- Shared fallback behavior stays the same:
  - CLI `--model` or env `OLLAMA_MODEL` set the base model for the whole app.
- Optional per-agent env overrides now exist for the LLM-backed nodes/helpers:
  - `OLLAMA_MODEL_INTENT_ROUTER`
  - `OLLAMA_MODEL_TASK_PLANNER`
  - `OLLAMA_MODEL_CODE_GENERATOR`
  - `OLLAMA_MODEL_CODE_REFINER`
  - `OLLAMA_MODEL_VALIDATION_FIXER`
  - `OLLAMA_MODEL_VERIFICATION_FIXER`
  - `OLLAMA_MODEL_REQUIREMENTS_VERIFIER`
  - `OLLAMA_MODEL_SOLUTION_EXPLAINER`
  - `OLLAMA_MODEL_QUESTION_ANSWERER`
- Prompt audit logs now record both `agent_name` and the effective `model` used for that request, which makes per-agent routing visible in saved logs.
