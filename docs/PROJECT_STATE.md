# "Текущее состояние"

# Project state

## Current status
Репозиторий очищен до одного канонического runtime: `app.py + src/graph`.
Система теперь умеет не только генерировать и дорабатывать Lua-код, но и работать с путями:
- принимать explicit `.lua` path;
- принимать директорию и создавать внутри неё slug-based папку/файл;
- переиспользовать активный Lua target в рамках чата;
- сохранять итоговый Lua-файл на диск после успешного прохода pipeline.

## What works
- единый LangGraph pipeline без второго orchestration path
- web UI через `app.py`
- chat persistence через SQLite
- target resolution для Lua file / directory / active target / fallback slug target
- generate
- refine
- local validate через `lua` + `luacheck`
- fix loop
- requirement verification через тот же LLM provider
- сохранение результата в целевой `.lua` файл
- команды `/path`, `/status`, `/code`, `/prompt`, `/new`, `/edit`, `/retry`

## What was removed
- `main.py` как второй runtime
- `edit_file.py`
- generic README/text artifact orchestration
- прямые LLM client paths через legacy root-level модули

## What is still incomplete
- финальная Ollama-конфигурация под требования хакатона
- фиксация exact demo-scenario для жюри
- подтверждение VRAM-лимита `<= 8 GB`
- автоматизированные regression tests

## Runtime status
- текущий runtime локальный и OpenAI-compatible
- по умолчанию ориентирован на `http://127.0.0.1:1234/v1`
- это dev/runtime-состояние, а не финальный Ollama-target

## Demo reproducibility
- README теперь описывает запуск canonical runtime
- для реального воспроизведения всё ещё нужны локально установленные зависимости:
  - Python packages из `requirements.txt`
  - `lua`
  - `luacheck`
  - локальный OpenAI-compatible runtime

## Open risks
- path heuristics зависят от формулировки prompt и пока не покрыты автоматическими тестами
- если local LLM runtime недоступен, generation/refine/verify path не работает
- статус Ollama для хакатона пока не закрыт

## Next tasks
- перевести canonical runtime с текущего local OpenAI-compatible endpoint на Ollama
- зафиксировать demo-scenario и smoke-check команды для жюри
- добавить хотя бы базовые automated regression checks для target resolution и pipeline flow
