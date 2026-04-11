# Общий контекст проекта

# LocalScript / LowCode Lua Script Builder

## Canonical runtime
- Единственный пользовательский entry point: `app.py`
- Веб-рантайм хранит чаты в SQLite и на каждый turn вызывает `PipelineEngine.process_message()`
- Единственный orchestration path: `src/graph/*`
- Единый LLM abstraction layer: `src/core/llm.py`

## Канонический pipeline
`resolve_target -> route_intent -> generate|refine|answer -> validate -> verify -> save -> explain_solution -> respond`

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
- `validate_code` запускает локальную диагностику через `lua` в temporary LowCode harness:
  - создаёт mock `wf.vars` / `wf.initVariables`;
  - добавляет `_utils.array.*` stubs;
  - строит nested mock paths для найденных `wf.vars.*` / `wf.initVariables.*`, включая alias-derived field access;
  - извлекает общие runtime repair hints из типовых Lua ошибок для следующего fix-шага.
- `fix_code` выполняет итеративные правки по стадии ошибки:
  - validation;
  - requirements;
- `verify_requirements` — семантическая LLM-проверка соответствия исходному запросу.
- Для задач cleanup/remove/filter по ключам в workflow object/array deterministic guard требует реальную трансформацию данных, а не простой `return` исходного пути.
- Если в тексте задачи указан bare field name без полного `wf.vars.*` / `wf.initVariables.*`, compiler пытается однозначно разрешить его через parseable workflow context и добавить в expected workflow paths.
- Если intent классифицирован как `change`, но текущий код отсутствует, pipeline идёт в `generate_code`, а не в `refine_code`.
- `save_code` выполняется только после успешной локальной валидации и проверки требований.
- если новый чат не содержит явного path и active target ещё не выбран, `save_code` не пишет файл и помечает save как intentionally skipped.
- `save_code` сохраняет два артефакта:
  - чистый `.lua` файл в canonical target path;
  - sidecar `*.jsonstring.txt` с представлением `lua{...}lua` рядом с ним.
- `explain_solution` формирует:
  - краткое объяснение;
  - что есть в коде;
  - как работает;
  - 1-3 предложения улучшений;
  - 1-3 уточняющих вопроса.
- `prepare_response` собирает финальный ответ для чата с кодом, статусами проверок и объяснением.

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
  - verification helper;
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

## 2026-04-11 update: public-sample alignment
- Prompt assembly now splits the user message into `task` and pasted workflow context.
- Generation, refine, and fix prompts now include embedded few-shot examples derived from the public sample and enforce short workflow-script style.
- The canonical style is: direct `wf.vars` / `wf.initVariables` access, no recreated demo input tables, no console wrappers, and direct `return` for simple extraction/computation tasks.
- Deterministic verification now supplements LLM verification with:
  - expected workflow path matching;
  - anti-pattern detection for invented sample data and app/service wrappers;
  - save-gate blocking when deterministic requirements fail, even if the semantic verifier is unavailable.
- `docs/_pdf_text.txt` remains an offline working aid only and is not read at runtime.

## 2026-04-11 update: compiled workflow context
- The pipeline now has an explicit generation-context preparation stage before `generate_code` / `refine_code`.
- Parsed workflow JSON is compiled into an internal request object with:
  - path inventory;
  - path types;
  - sample values;
  - selected operation;
  - selected primary path;
  - ranked candidate paths;
  - deterministic simple-task code when available.
- Simple single-target data tasks now bypass main LLM generation and are compiled directly into minimal workflow Lua, for example:
  - count array -> `return #wf.vars.path`
  - first/last element -> `return wf.vars.path[1]` / `return wf.vars.path[#wf.vars.path]`
  - direct scalar extraction -> `return wf.vars.path`
- If the workflow path cannot be selected confidently, the pipeline stops before generation, asks a clarification question, and does not save code.
