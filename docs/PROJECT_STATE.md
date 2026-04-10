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
  - direct access вместо JsonPath
  - `wf.vars` / `wf.initVariables` для данных схемы
- локальная валидация через `lua`
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
- migration на Ollama как финальный runtime
- фиксация финального demo flow под жюри
- подтверждение VRAM-лимита `<= 8 GB` на целевом конфиге
- возврат e2e-gate после отдельного решения по нему
- автоматизированные regression tests (пока только smoke/manual checks)

## Runtime status
- локальный OpenAI-compatible endpoint
- default `base_url`: `http://127.0.0.1:1234/v1`
- текущий runtime валиден как dev-state, но не как финальный Ollama-target

## Demo reproducibility
README описывает канонический запуск через `app.py` и текущий pipeline.
Для воспроизведения нужны:
- Python deps из `requirements.txt`
- `lua`
- доступный локальный LLM endpoint

## Open risks
- эвристики path resolution пока не покрыты автоматическими тестами
- при недоступности LLM часть шагов (generate/verify/explain) недоступна
- без e2e-gate сохранение теперь опирается только на локальную валидацию и LLM verification
- без `luacheck` lint-класс проблем сейчас не отлавливается отдельным шагом
- naming эвристики остаются rule-based и могут потребовать дальнейшей подстройки под реальные prompt patterns

## Next tasks
- перевести canonical runtime на Ollama-конфигурацию хакатона
- добавить автотесты для:
  - target resolution;
  - save gate без e2e;
  - follow-up применения предложений
