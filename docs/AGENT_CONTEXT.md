# "Общий контекст проекта"

# LocalScript / Lua Console Builder

## Canonical runtime
- Единственный пользовательский entry point: `app.py`
- `app.py` поднимает локальный web UI, хранит чаты в SQLite и на каждый user turn вызывает `PipelineEngine.process_message()`
- `src/graph/` содержит единственный orchestration pipeline
- `src/core/llm.py` — единый async provider для локального OpenAI-compatible runtime

## Pipeline
`resolve_target -> route_intent -> generate|refine|answer -> validate -> verify|fix -> save -> respond`

### Что делает pipeline
- `resolve_target` выбирает активный Lua target:
  - explicit `.lua` path из prompt;
  - директорию из prompt, внутри которой создается slug-based папка и `.lua` файл;
  - уже активный target текущего чата;
  - fallback target по slug, если это новая генерация без пути
- `route_intent` различает create/change/question-like сценарии
- `generate_code` делает первичную генерацию Lua
- `refine_code` меняет существующий Lua-файл, сохраняя функции, если их не просили удалить
- `validate_code` запускает локальные проверки через `lua` и `luacheck`
- `fix_code` делает LLM-driven fix loop по диагностике
- `verify_requirements` проверяет соответствие исходной задаче через тот же LLM provider
- `save_code` пишет итоговый Lua-файл на диск только после успешного прохода по “хорошей” ветке
- `prepare_response` собирает ответ пользователю и показывает статус/путь сохранения

## Ownership по модулям
- `app.py`
  - web UI
  - chat store
  - командный слой `/new`, `/edit`, `/retry`, `/code`, `/path`, `/status`, `/prompt`
  - хранение active target path и workspace root в chat state
- `src/core/llm.py`
  - единая конфигурация `model/base_url/timeout`
  - все LLM-вызовы идут через этот provider
- `src/graph/`
  - узлы pipeline и edge conditions
  - единственная поддерживаемая orchestration логика
- `src/tools/target_tools.py`
  - разбор путей из prompt
  - slug generation
  - target resolution
  - чтение/сохранение Lua-файла
- `src/tools/lua_tools.py`
  - нормализация Lua output
  - локальная диагностика
  - verification helper
  - preservation guard
- `src/tools/local_runtime.py`
  - низкоуровневые wrappers для `lua` и `luacheck`

## Product boundaries
- Продуктовый scope — только Lua-oriented сценарии
- Generic README/text artifact generation удалена из runtime
- Generic file editor и второй orchestration path удалены
- `main.py` больше не является рабочим entry point

## Runtime dependencies
- Windows / Python 3.12+
- локальный OpenAI-compatible endpoint по умолчанию: `http://127.0.0.1:1234/v1`
- `lua` в PATH
- `luacheck` в PATH

## Important note
- Текущий canonical runtime локальный и единый, но это ещё не финальный Ollama-target для хакатона
