# LocalScript / LowCode Lua Script Builder

Локальный web runtime для генерации, доработки, валидации и сохранения workflow/LUS Lua-скриптов.

## Что делает проект
- принимает задачу на естественном языке;
- генерирует или дорабатывает LowCode workflow/LUS script на `Lua 5.5`;
- запускает локальную проверку через `lua` в temporary LowCode harness;
- делает fix loop при ошибках;
- выполняет modular LLM-проверку через цепочку verifier-агентов и общий post-verification fixer;
- если в чате указан явный target path или уже есть active target, сохраняет код после успешной локальной валидации и проверки требований:
  - как чистый целевой `.lua` файл;
  - как sidecar JSON-артефакт рядом с ним, где значение поля содержит JsonString `lua{...}lua`;
- возвращает:
  - код;
  - user-facing JSON payload с embedded `lua{...}lua`;
  - путь сохранения, если он был задан;
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
- [Ollama](https://ollama.com/) (локальный LLM runtime)
- `lua` в PATH

## Установка

### 1. Python-зависимости
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. Ollama и модель

Скачать и установить Ollama: https://ollama.com/download

```powershell
# Скачать модель (хакатонная конфигурация, 8 GB VRAM)
ollama pull qwen2.5-coder:7b-instruct

# Для слабых GPU (4 GB VRAM) — меньшая модель
ollama pull qwen2.5-coder:3b-instruct
```

Опционально — создать кастомную модель с зафиксированными параметрами хакатона:

```powershell
ollama create truehack -f Modelfile
```

## Запуск

```powershell
# Стандартный запуск (модель по умолчанию: qwen2.5-coder:7b-instruct)
python app.py --workspace C:\Work\LuaProjects

# С кастомной моделью из Modelfile
python app.py --model truehack

# С меньшей моделью (4 GB VRAM)
python app.py --model qwen2.5-coder:3b-instruct

# Все параметры
python app.py --host 127.0.0.1 --port 8765 --workspace C:\Work\LuaProjects --model qwen2.5-coder:7b-instruct --url http://127.0.0.1:11434/v1
```

Переменные окружения (альтернатива CLI-аргументам):
- `OLLAMA_MODEL` — имя модели (по умолчанию `qwen2.5-coder:7b-instruct`)
- `OLLAMA_BASE_URL` — URL Ollama API (по умолчанию `http://127.0.0.1:11434/v1`)

Опционально можно задать отдельные модели для конкретных LLM-агентов через `.env`.
Приоритет выбора модели такой:
1. `OLLAMA_MODEL_<AGENT_NAME>` для конкретного агента
2. CLI `--model`
3. общий `OLLAMA_MODEL`
4. встроенный default `qwen2.5-coder:7b-instruct`

Поддерживаемые per-agent переменные:
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

Пример:

```env
OLLAMA_MODEL=qwen2.5-coder:7b-instruct
OLLAMA_MODEL_INTENT_ROUTER=qwen2.5-coder:3b-instruct
OLLAMA_MODEL_SOLUTION_EXPLAINER=qwen2.5-coder:3b-instruct
```

## Как задавать путь для Lua

### 1. Явный `.lua` файл
Пример:

```text
Создай скрипт заметок в C:\Work\LuaProjects\notes.lua
```

Система сохранит итог именно в этот файл и рядом создаст sidecar `*.jsonstring.txt`.

### 2. Директория
Пример:

```text
Преобразуй DATUM и TIME из wf.vars.json.IDOC.ZCDF_HEAD в ISO дату в папке C:\Work\LuaProjects
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

### 4. Без явного пути
Пример:

```text
Верни последний email из wf.vars.emails
```

Система выполнит полный цикл `generate/refine -> validate -> modular verification chain -> explain/respond`, но не будет создавать `.lua` файл и sidecar, если в этом чате ещё нет active target.

## LowCode contract
- generation target: `Lua 5.5`
- script description format in prompts/user-facing output: `lua{ ... }lua`
- `JsonPath` использовать нельзя; доступ к данным должен быть прямым
- declared variables: `wf.vars`
- startup variables from `variables`: `wf.initVariables`
- это workflow/LUS script, а не console/CLI program
- скрипт должен возвращать значение и/или обновлять `wf.vars`
- console input/output (`io.read`, `print`, `io.write`) по умолчанию не используются
- массивы создаются/маркируются через `_utils.array.new()` и `_utils.array.markAsArray(arr)`
- для shape-sensitive задач массивом считается только table с числовыми ключами `1..n` без пропусков; table со строковыми ключами вроде `name` / `phone` массивом не считается; пустая table считается массивом
- базовые конструкции: `if`, `while`, `for`, `repeat`

Во время локальной validation runtime поднимает временный harness:
- создаёт mock `wf.vars` и `wf.initVariables`;
- добавляет `_utils.array.new()` и `_utils.array.markAsArray(arr)`;
- автоматически строит nested mock-таблицы для найденных цепочек `wf.vars.*` и `wf.initVariables.*`, включая aliased access patterns.

## Pipeline
Основная ветка:

```text
resolve_target -> route_intent -> generate/refine -> validate -> verify_contract -> verify_shape_type -> verify_semantic_logic -> verify_runtime_state -> verify_robustness -> save -> explain_solution -> respond
```

Если проваливается validation/modular verification:
- при runtime/syntax fail запускается `fix_validation_code`;
- при failed verifier запускается `fix_verification_issue`;
- затем pipeline повторяет нужную часть цикла проверок;
- если лимит fix-итераций исчерпан, файл не сохраняется.

## Что видно в ответе
- user-facing JSON payload, где значение поля содержит `lua{ ... }lua`;
- статус local validation / verification;
- путь сохранения `.lua` и sidecar JsonString, если сохранение выполнялось;
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

## Runtime

Canonical runtime — Ollama с OpenAI-compatible API на `http://127.0.0.1:11434/v1`.

Модель по умолчанию: `qwen2.5-coder:7b-instruct`.
Параметры хакатона зафиксированы в `Modelfile` (num_ctx=4096, num_predict=256, num_gpu=99).

Смена модели:
- CLI: `python app.py --model qwen2.5-coder:3b-instruct`
- ENV: `OLLAMA_MODEL=qwen2.5-coder:3b-instruct`
- Modelfile: `ollama create mymodel -f Modelfile` + `--model mymodel`

Заметки:
- `e2e`-агент и e2e-gate сейчас временно отключены;
- `luacheck` сейчас не используется в каноническом runtime;
- generation/refine/fix ориентированы на workflow/LUS scripts, а не на console/CLI apps;
- `route_intent` теперь hybrid:
  - сначала учитывает реальное наличие кода в чате, clarification state и deterministic text signals;
  - LLM intent classifier используется как fallback для неоднозначных случаев;
  - если кода ещё нет и пользователь не прислал Lua в сообщении, change-like wording (`исправь`, `улучши`, `оберни`, `очисти`) трактуется как `create`, а не как `change`;
  - если Lua прислан прямо в сообщении, он может стать входным кодом для `change` / `inspect`;
- generation/refine/fix теперь остаются model-driven:
  - compiler подготавливает workflow inventory, path hints и clarification gate;
  - финальный Lua-код всегда синтезирует модель по текущей задаче и контексту;
  - prompts больше не подталкивают каждую задачу к shortest-return шаблону и допускают multi-step scripts, loops, guards и helper functions, когда это нужно задаче;
  - prompts теперь используют один жёсткий format contract: ответ должен начинаться с `lua{` и заканчиваться `}lua`, без кавычек и без code fences;
  - в generation/fix prompts сокращён служебный шум: вместо ranked candidates / confidence / длинных diagnostics модель получает задачу, основной workflow path, текущий context и короткий список обязательных исправлений;
  - `fix_validation_code` больше не подсовывает модели прошлый сломанный assistant output и переписывает сценарий по short mandatory-fix list;
  - generate/refine/fix теперь используют более консервативную temperature policy для parseable workflow context и shape-sensitive tasks;
  - raw LLM output считается невалидным, если модель вернула fences, quoted wrapper или не начала ответ с `lua{`;
- verification теперь использует modular verdict через unified structured outputs verifier-агентов и внешний aggregate `verification` / `verification_passed`;
- verifier теперь получает concrete runtime evidence:
  - updated workflow snapshot after execution for mutation scripts;
  - contradiction-focused second pass can overturn an optimistic false positive;
  - verifier дополнительно не должен одобрять ответы с markdown/quoted wrappers и shape-sensitive код, где `next(...)` выступает единственной проверкой массива;
- shape-sensitive задачи теперь проверяются semantic verifier'ом и fix-loop по явным правилам:
  - нельзя опираться только на `type(x) == "table"` там, где нужно отличать object-like table от array-like table;
  - проверки `next(x)` / empty-vs-non-empty не считаются достаточным shape detection;
  - нельзя просто пометить исходный workflow object/scalar как массив через `_utils.array.markAsArray(source)` вместо создания нового array value;
  - `_utils.array.new()` должен вызываться без inline arguments;
  - для новых массивов требуется `_utils.array.markAsArray(arr)`;
- naming для auto-created folder/file и title чата строится из очищенного prompt и санитизируется под Windows;
- без явного пути в новом чате runtime больше не создаёт fallback file target и показывает код только в ответе;
- задачи очистки/удаления ключей внутри workflow-объектов больше не считаются простым `return`-сценарием и проходят semantic verification на реальную трансформацию данных;
- если пользователь в задаче упоминает bare field name из parseable workflow context, runtime пытается однозначно привязать его к `wf.vars.*` или `wf.initVariables.*` и использует это в verification/save gate;
- fix-loop теперь получает не только raw Lua runtime error, но и нормализованные repair hints для типовых ошибок аргументов, nil access/call, arithmetic/type mismatch и concatenation;
- `fix_validation_code` теперь делает один внутренний stricter retry, если первый fix-ответ:
  - не является внятным standalone Lua;
  - почти не меняет код;
  - или детерминированно повторяет те же requirement failures;
- normalizer умеет извлекать Lua не только из `lua{...}lua`, но и из structured model envelopes вроде JSON-объектов с полями `lua` / `code` / `script`, чтобы validation не падала на сыром мета-формате ответа;
- этот extraction работает и для fenced/meta-wrapped JSON envelopes, если модель вернула ` ```json { ... "field": "lua{...}lua" } ``` ` вместо чистого Lua;
- README фиксирует текущее состояние кода, а не желаемое будущее.
