# LocalScript / Lua Console Builder

Локальный web runtime для генерации, доработки и валидации Lua-скриптов.

## Что делает проект
- принимает задачу на естественном языке;
- генерирует или дорабатывает Lua-код;
- локально валидирует результат через `lua` и `luacheck`;
- делает fix loop по диагностике;
- умеет работать с путями:
  - explicit `.lua` file path;
  - directory path, внутри которой создаёт подпапку и `.lua` файл;
  - active target текущего чата.

## Canonical runtime
Единственный поддерживаемый entry point:

```powershell
python app.py
```

`main.py` больше не используется.

## Требования
- Python 3.12+
- локальный OpenAI-compatible LLM runtime
- `lua` в PATH
- `luacheck` в PATH

## Установка
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Запуск
```powershell
python app.py --workspace C:\Users\Dimentiy\repoVScode\TrueHack
```

Полезные параметры:

```powershell
python app.py --host 127.0.0.1 --port 8765 --workspace C:\Work\LuaProjects
python app.py --url http://127.0.0.1:1234/v1 --model local-model
```

Переменные окружения:
- `LOCAL_LLM_BASE_URL`
- `LOCAL_LLM_MODEL`

Для обратной совместимости код также читает `LMSTUDIO_URL` и `LMSTUDIO_MODEL`, но canonical docs ориентированы на generic local OpenAI-compatible runtime.

## Как задавать пути

### 1. Явный `.lua` файл
Пример:

```text
Создай скрипт заметок в C:\Work\LuaProjects\notes.lua
```

Что произойдет:
- активным target станет именно `C:\Work\LuaProjects\notes.lua`
- итоговый код будет сохранён в этот файл

### 2. Директория
Пример:

```text
Создай текстовую игру в папке C:\Work\LuaProjects
```

Что произойдет:
- система построит slug из prompt;
- внутри указанной директории создаст подпапку;
- внутри подпапки создаст итоговый `.lua` файл.

Примерная форма пути:

```text
C:\Work\LuaProjects\<slug>\<slug>.lua
```

### 3. Продолжение в том же чате
Если после первой генерации написать:

```text
Добавь сохранение в файл
```

система переиспользует active target текущего чата и сохранит изменения в тот же `.lua` файл.

## Команды в UI
- `/new <задача>` — начать новый проект в текущем чате
- `/edit <изменение>` — изменить текущий код
- `/retry` — повторно проверить и попробовать исправить текущий код
- `/code` — показать текущий Lua-код
- `/path` — показать активный Lua target и workspace
- `/status` — показать текущий статус
- `/prompt` — показать базовую задачу и накопленные правки

## Что считается успешным проходом pipeline
Основная “хорошая” ветка:

```text
resolve_target -> route_intent -> generate/refine -> validate -> verify -> save -> respond
```

Если локальная валидация не прошла, pipeline идёт в fix loop.
Если fix loop исчерпан и код остаётся проблемным, ответ пользователю формируется без финального сохранения файла.

## Текущий runtime status
Сейчас canonical runtime:
- локальный;
- работает через OpenAI-compatible endpoint;
- по умолчанию смотрит в `http://127.0.0.1:1234/v1`.

Важно:
- это ещё не финальная Ollama-конфигурация под хакатон;
- README честно описывает текущее состояние репозитория, а не желаемое будущее.

## Что ещё не закрыто под хакатон
- migration на Ollama
- фиксация exact demo flow для жюри
- подтверждение VRAM-лимита `<= 8 GB`
