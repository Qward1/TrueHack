# Общий контекст проекта

# LocalScript / Lua Console Builder

## Canonical runtime
- Единственный пользовательский entry point: `app.py`
- Веб-рантайм хранит чаты в SQLite и на каждый turn вызывает `PipelineEngine.process_message()`
- Единственный orchestration path: `src/graph/*`
- Единый LLM abstraction layer: `src/core/llm.py`

## Канонический pipeline
`resolve_target -> route_intent -> generate|refine|answer -> validate -> verify -> save -> explain_solution -> respond`

### Поведение pipeline
- `resolve_target`:
  - explicit `.lua` path;
  - директория -> slug-папка + slug.lua;
  - active target текущего чата;
  - fallback target для нового create turn без пути.
  - невалидные Windows-сегменты пути санитизируются до сохранения.
- `generate_code` / `refine_code` возвращают полный Lua-файл.
- `validate_code` запускает локальную диагностику через `lua`.
- `fix_code` выполняет итеративные правки по стадии ошибки:
  - validation;
  - requirements;
- `verify_requirements` — семантическая LLM-проверка соответствия исходному запросу.
- `save_code` выполняется только после успешной локальной валидации и проверки требований.
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
  - path parsing/resolution и сохранение Lua-файла.
  - naming/sanitization helpers для auto-created folder/file и chat title.
- `src/tools/lua_tools.py`:
  - нормализация Lua-ответа;
  - локальная диагностика;
  - verification helper;
  - временно неиспользуемые e2e helpers для будущего возврата e2e-gate.
- `src/tools/local_runtime.py`:
  - низкоуровневые wrappers для `lua`;
  - сохраненные, но неиспользуемые wrappers для `luacheck`;
  - запуск с stdin для проверки интерактивных сценариев.

## Runtime зависимости
- Python 3.12+
- `lua` в PATH
- локальный OpenAI-compatible endpoint (по умолчанию `http://127.0.0.1:1234/v1`)

## Важно
- Ollama migration под финальные требования хакатона пока не завершен.
- Текущий runtime локальный и единый, но это dev-state, а не финальный Ollama-target.
- E2E agent flow временно отключен в каноническом pipeline.
- `luacheck` временно отключен в канонической локальной валидации.
