# Общий контекст проекта

# LocalScript / LowCode Lua Script Builder

## Canonical runtime
- Единственный пользовательский entry point: `app.py`
- Веб-рантайм хранит чаты в SQLite и на каждый turn вызывает `PipelineEngine.process_message()`
- Единственный orchestration path: `src/graph/*`
- Единый LLM abstraction layer: `src/core/llm.py`

## Канонический pipeline
`resolve_target -> route_intent -> generate|refine|answer -> validate -> verify_contract -> verify_shape_type -> verify_semantic_logic -> verify_runtime_state -> verify_robustness -> save -> explain_solution -> respond`

### Поведение pipeline
- Generated/refined/fixed Lua now targets the LowCode contract:
  - target version: `Lua 5.5`;
  - script description format: `lua{ ... }lua`;
  - workflow/LUS script instead of console/CLI app;
  - без `JsonPath`, только прямой доступ к данным;
  - declared variables: `wf.vars`;
  - startup variables: `wf.initVariables`.
- `resolve_target`:
  - explicit `.lua` path;
  - директория -> slug-папка + slug.lua;
  - active target текущего чата;
  - невалидные Windows-сегменты пути санитизируются до сохранения.
- `generate_code` / `refine_code` возвращают полный Lua-файл.
- Генерация и fix/refine теперь model-driven:
  - compiler даёт path inventory, ranked candidates и clarification gate;
  - финальный скрипт всегда синтезирует LLM по текущей задаче и контексту;
  - prompt contract больше не заставляет все задачи схлопываться в короткий `return` и допускает multi-step workflow scripts, loops, guards, table traversal и helper functions, когда это требуется логикой;
  - prompts теперь используют один format contract: ответ обязан начинаться с `lua{` и заканчиваться `}lua`, без кавычек и без code fences;
  - service noise в generation/fix prompt сокращён: без ranked candidates / confidence / длинных diagnostic dumps;
  - `fix_validation_code` не подсовывает модели прошлый assistant output с ошибочным кодом, а даёт короткий список mandatory fixes и заставляет переписать скрипт заново;
  - generate/refine/fix use a lower temperature for parseable workflow context to reduce wrapper noise and shallow shortcut generations.
- `src/tools/lua_tools.py` normalizer дополнительно умеет доставать Lua из structured LLM responses, если модель вернула JSON envelope с полем `lua` / `code` / `script`, а не чистый code body, включая fenced/meta-wrapped JSON payloads.
- `validate_code` запускает локальную диагностику через `lua` в temporary LowCode harness:
  - создаёт mock `wf.vars` / `wf.initVariables`;
  - добавляет `_utils.array.*` stubs;
  - строит nested mock paths для найденных `wf.vars.*` / `wf.initVariables.*`, включая alias-derived field access;
  - извлекает общие runtime repair hints из типовых Lua ошибок для следующего fix-шага.
- validation hint analysis (`CodeValidator`) now uses the numbered script, runtime traceback, and the exact validation workflow context together; its prompt requires `root cause`, `why it fails on this context`, and an `exact repair path` instead of a bare traceback restatement.
- `fix_validation_code` выполняет итеративные правки по стадии ошибки:
  - validation;
  - если первый fix-ответ остаётся пустым, почти не меняет код или детерминированно повторяет тот же runtime failure, node делает один stricter internal retry before returning to validate.
- validation-fix prompt intentionally keeps only runtime-failure context: `run_error`, line/context hints, `llm_fix_hint`, and the current numbered code block, without task/planner/workflow-context payload from `compiled_request`.
- legacy semantic verifier node удалён из active graph.
- active modular verification contour живёт в `src/agents/*` и использует тот же внешний aggregate verdict (`verification`, `verification_passed`, `summary`, `missing_requirements`, `warnings`) через stage-by-stage orchestration в `src/agents/verification_chain.py`.
- runtime/state evidence из validation harness сохраняется в pipeline state для modular verifier-агентов и их общего fixer-а.
- Если в тексте задачи указан bare field name без полного `wf.vars.*` / `wf.initVariables.*`, compiler пытается однозначно разрешить его через parseable workflow context и добавить в expected workflow paths.
- Если intent классифицирован как `change`, но текущий код отсутствует, pipeline идёт в `generate_code`, а не в `refine_code`.
- `route_intent` теперь hybrid:
  - сначала использует deterministic signals из chat state и текста запроса;
  - только затем обращается к LLM как к tie-breaker для неоднозначных случаев;
  - без existing code в чате и без pasted Lua в текущем сообщении change-like wording (`исправь`, `улучши`, `очисти`, `оберни`) трактуется как `create`, а не `change`;
  - если пользователь прислал Lua прямо в сообщении, этот код считается доступным контекстом для `change` / `inspect`.
- `save_code` выполняется только после успешной локальной валидации и полного modular verification chain.
- если новый чат не содержит явного path и active target ещё не выбран, `save_code` не пишет файл и помечает save как intentionally skipped.
- `save_code` сохраняет два артефакта:
  - чистый `.lua` файл в canonical target path;
  - sidecar `*.jsonstring.txt` как JSON-объект, где value содержит `lua{...}lua`.
- `explain_solution` формирует:
  - краткое объяснение;
  - что есть в коде;
  - как работает;
  - 1-3 предложения улучшений;
  - 1-3 уточняющих вопроса.
- `prepare_response` собирает финальный ответ для чата с JSON payload, статусами проверок и объяснением.

## Chat-level поведение
- `app.py` сохраняет:
  - `target_path`, `workspace_root`, `current_code`, `base_prompt`, `change_requests`;
  - `last_suggested_changes`, `last_clarifying_questions`, `last_e2e_summary`.
- title чата строится из очищенного prompt и при наличии target не зависит от сырого пути пользователя.
- Follow-up поддерживает ссылки на предложения:
  - пример: `примени предложение 1`;
  - система разворачивает это в явный change request и запускает следующий refine-cycle.

## Ownership по модулям
- `app.py`:
  - web UI;
  - chat persistence;
  - команды `/new`, `/edit`, `/retry`, `/code`, `/path`, `/status`, `/prompt`;
  - state bridge между UI и pipeline.
- `src/core/llm.py`:
  - конфигурация local OpenAI-compatible endpoint;
  - единые методы generate/chat/json.
- `src/graph/`:
  - состояние, узлы, условия переходов и компоновка pipeline.
- `src/tools/target_tools.py`:
  - path parsing/resolution и dual-save артефактов (`.lua` + JsonString sidecar).
  - naming/sanitization helpers для auto-created folder/file и chat title.
- `src/tools/lua_tools.py`:
  - нормализация Lua-ответа;
  - локальная диагностика и LowCode validation harness;
  - временно неиспользуемые e2e helpers для будущего возврата e2e-gate.
- `src/tools/local_runtime.py`:
  - низкоуровневые wrappers для `lua`;
  - сохраненные, но неиспользуемые wrappers для `luacheck`;
  - запуск с stdin сохранен только для legacy/unused interactive scenarios.

## Runtime зависимости
- Python 3.12+
- `lua` в PATH
- Ollama (по умолчанию `http://127.0.0.1:11434/v1`)
- Модель: `qwen2.5-coder:7b-instruct` (или `3b-instruct` для 4GB VRAM)

## Настройка модели
Три способа (по приоритету):
1. CLI аргумент: `python app.py --model <name>`
2. Env-переменная: `OLLAMA_MODEL=<name>`
3. Дефолт: `qwen2.5-coder:7b-instruct`

Параметры хакатона (num_ctx=4096, num_predict=256) зафиксированы в `Modelfile`.

## Важно
- E2E agent flow временно отключен в каноническом pipeline.
- `luacheck` временно отключен в канонической локальной валидации.

---

## 2026-04-11 update: model-driven workflow synthesis
- Prompt assembly splits the user message into `task` and pasted workflow context.
- Generation, refine, and fix prompts now use abstract synthesis guidance instead of embedded code templates/few-shot snippets.
- The canonical style is: direct `wf.vars` / `wf.initVariables` access, no recreated demo input tables, no console wrappers, and the amount of Lua structure the task actually needs.
- Modular verification is now the active verification contour:
  - standalone verifiers/fixer keep the aggregate verdict fields `passed`, `summary`, `missing_requirements`, and `warnings`;
  - orchestration state for that contour lives in `verification_chain_*`;
  - active graph wiring now routes successful validation into `verify_contract` and then through the full stage chain plus `fix_verification_issue`.
- `docs/_pdf_text.txt` remains an offline working aid only and is not read at runtime.

## 2026-04-11 update: compiled workflow context
- The pipeline now has an explicit generation-context preparation stage before `generate_code` / `refine_code`.
- Parsed workflow JSON is compiled into an internal request object with:
  - path inventory;
  - sample values;
  - selected operation;
  - selected primary path;
  - ranked candidate paths.
- The compiled request is now used to steer model generation and verification, not to emit canned Lua templates directly.
- If the workflow path cannot be selected confidently, the pipeline stops before generation, asks a clarification question, and does not save code.

## 2026-04-12 update: validation and verification-contour prep
- Workflow-context parsing now tolerates loose pasted JSON fragments and fenced JSON-like blocks before the compiler builds the request.
- `src/tools/lua_tools.py` normalizes malformed LowCode wrappers before validation, including fenced `lua{...}lua` variants and partial trailing wrapper noise.
- Runtime marker extraction now tolerates both LF and CRLF output from the temporary Lua harness.
- `validate_code` now captures both the actual returned value and the updated workflow snapshot from the temporary workflow harness.
- Those runtime artifacts are preserved in state for the standalone modular verification contour under `src/agents/*`.
- `explain_solution` now normalizes explainer sections from either JSON arrays or single strings before falling back to generic text.
- `prepare_response` always includes the current code payload in the user response, even on failed validation turns; the response stays diagnostic and saving can still be skipped.

## 2026-04-12 update: per-agent model overrides
- `src/core/llm.py` remains the single LLM abstraction layer, but each LLM-backed agent can now use its own Ollama model via env without adding a second client path.
- Override priority is:
  - `OLLAMA_MODEL_<AGENT_NAME>` for a specific agent;
  - CLI `--model`;
  - shared fallback `OLLAMA_MODEL`;
  - built-in default model.
- Supported agent-specific env keys in the canonical pipeline:
- `OLLAMA_MODEL_INTENT_ROUTER`
- `OLLAMA_MODEL_TASK_PLANNER`
- `OLLAMA_MODEL_CODE_GENERATOR`
- `OLLAMA_MODEL_CODE_REFINER`
- `OLLAMA_MODEL_VALIDATION_FIXER`
- `OLLAMA_MODEL_CONTRACT_VERIFIER`
- `OLLAMA_MODEL_SHAPE_TYPE_VERIFIER`
- `OLLAMA_MODEL_SEMANTIC_LOGIC_VERIFIER`
- `OLLAMA_MODEL_RUNTIME_STATE_VERIFIER`
- `OLLAMA_MODEL_ROBUSTNESS_VERIFIER`
- `OLLAMA_MODEL_UNIVERSAL_VERIFICATION_FIXER`
- `OLLAMA_MODEL_SOLUTION_EXPLAINER`
- `OLLAMA_MODEL_QUESTION_ANSWERER`
