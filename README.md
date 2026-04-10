# LocalScript / Lua Console Builder

Локальный web runtime для генерации, доработки, валидации и сохранения Lua-скриптов.

## Что делает проект
- принимает задачу на естественном языке;
- генерирует или дорабатывает Lua-код;
- запускает локальную проверку через `lua`;
- делает fix loop при ошибках;
- выполняет LLM-проверку соответствия исходному запросу;
- сохраняет код в целевой `.lua` файл после успешной локальной валидации и проверки требований;
- возвращает:
  - код;
  - путь сохранения;
  - объяснение реализации;
  - предложения улучшений;
  - уточняющие вопросы.

## Canonical runtime
Единственный поддерживаемый entry point:

```powershell
python app.py
```

## Требования
- Python 3.12+
- локальный OpenAI-compatible LLM runtime
- `lua` в PATH

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

Дополнительно:

```powershell
python app.py --host 127.0.0.1 --port 8765 --workspace C:\Work\LuaProjects
python app.py --url http://127.0.0.1:1234/v1 --model local-model
```

Переменные окружения:
- `LOCAL_LLM_BASE_URL`
- `LOCAL_LLM_MODEL`

## Как задавать путь для Lua

### 1. Явный `.lua` файл
Пример:

```text
Создай скрипт заметок в C:\Work\LuaProjects\notes.lua
```

Система сохранит итог именно в этот файл.

### 2. Директория
Пример:

```text
Создай текстовую игру в папке C:\Work\LuaProjects
```

Система построит slug из prompt и создаст:

```text
C:\Work\LuaProjects\<slug>\<slug>.lua
```

Если в указанном пути есть невалидные для Windows символы в имени папки или файла, runtime автоматически санитизирует их перед сохранением.

### 3. Follow-up в том же чате
После первого turn можно писать:

```text
Добавь сохранение истории
```

Система переиспользует active target текущего чата.

## Pipeline
Основная ветка:

```text
resolve_target -> route_intent -> generate/refine -> validate -> verify -> save -> explain_solution -> respond
```

Если проваливается validation/verify:
- запускается `fix_code`;
- затем pipeline повторяет цикл проверок;
- если лимит fix-итераций исчерпан, файл не сохраняется.

## Что видно в ответе
- итоговый Lua-код;
- статус local validation / verification;
- путь сохранения;
- объяснение (что есть в коде и как работает);
- предложения улучшений;
- уточняющие вопросы.

## Follow-up по предложениям системы
Если в ответе есть список предложений, можно написать:

```text
Примени предложение 1
```

Система подставит это как явный change request и запустит новый refine-cycle.

## Команды UI
- `/new <задача>` — новый проект в текущем чате
- `/edit <изменение>` — доработать текущий код
- `/retry` — повторить полный цикл проверок
- `/code` — показать текущий Lua-код
- `/path` — показать active Lua target и workspace
- `/status` — показать статус чата
- `/prompt` — показать базовую задачу, правки и предложения

## Текущий runtime status
Сейчас canonical runtime:
- локальный;
- OpenAI-compatible;
- по умолчанию использует `http://127.0.0.1:1234/v1`.

Важно:
- это еще не финальная Ollama-конфигурация для хакатона;
- `e2e`-агент и e2e-gate сейчас временно отключены;
- `luacheck` сейчас не используется в каноническом runtime;
- naming для auto-created folder/file и title чата строится из очищенного prompt и санитизируется под Windows;
- README фиксирует текущее состояние кода, а не желаемое будущее.
