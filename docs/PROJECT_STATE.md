# Текущее состояние

# Project state

## Current status
Репозиторий работает в одном каноническом runtime (`app.py + src/graph`) и поддерживает полный цикл:

`generate/refine -> local validate -> fix -> requirement verify -> generated e2e -> run e2e -> save -> explain/respond`

Сохранение итогового `.lua` файла происходит только после успешного e2e этапа.

## Что работает
- единый LangGraph pipeline без дублирующего orchestration path
- path-aware Lua target logic:
  - explicit `.lua` path;
  - директория -> slug-папка + slug.lua;
  - active target в рамках чата
- генерация Lua-кода
- refine существующего Lua-кода
- локальная валидация (`lua` + `luacheck`)
- fix loop по стадиям ошибок
- LLM verification требований
- agent-generated e2e suite (JSON)
- запуск e2e кейсов с поддержкой `stdin`
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
- `luacheck`
- доступный локальный LLM endpoint

## Open risks
- генерация e2e suite зависит от доступности/стабильности local LLM
- эвристики path resolution пока не покрыты автоматическими тестами
- при недоступности LLM часть шагов (generate/verify/e2e suite/explain) недоступна

## Next tasks
- перевести canonical runtime на Ollama-конфигурацию хакатона
- добавить автотесты для:
  - target resolution;
  - e2e gate;
  - follow-up применения предложений
