# Текущее состояние

# Project state

## Current status
Репозиторий работает в одном каноническом runtime (`app.py + src/graph`) и поддерживает полный цикл:

`generate/refine -> local validate -> fix -> requirement verify -> save -> explain/respond`

Сохранение итогового кода происходит после успешной локальной валидации и проверки требований:
- canonical artifact: чистый `.lua` файл;
- дополнительный artifact: sidecar `*.jsonstring.txt` с `lua{...}lua`.

## Что работает
- единый LangGraph pipeline без дублирующего orchestration path
- path-aware Lua target logic:
  - explicit `.lua` path;
  - директория -> slug-папка + slug.lua;
  - active target в рамках чата
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
- LLM verification требований
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
