# TrueHach

Локальная агентская система на `LangGraph` для генерации, запуска, тестирования и ремонта `Lua`-скриптов.

## Быстрый старт

### Проверенный сценарий: Windows + LM Studio + `lua.exe` + `luacheck`

Это основной и уже проверенный способ запуска для текущего проекта.

Что должно быть запущено и установлено:

1. В `LM Studio` должна быть загружена модель и включен `Local Server`.
2. Должны работать команды:
   - `where lua`
   - `where luacheck`
   - `where py`

Быстрая проверка:

```cmd
cd C:\Users\Admin\Desktop\TrueHach
py main.py --check-runtime --prompt test
```

Если все в порядке, ты увидишь примерно такой результат:

```json
{
  "backend_preference": "auto",
  "selected_backend": "lua",
  "lua_path": "C:\\Users\\Admin\\AppData\\Local\\Programs\\Lua\\bin\\lua.EXE",
  "luajit_path": null,
  "linter": "C:\\Users\\Admin\\AppData\\Roaming\\luarocks\\bin\\luacheck",
  "lupa_available": false,
  "ready": true
}
```

Обычный запуск:

```cmd
cd C:\Users\Admin\Desktop\TrueHach
py main.py
```

После этого программа сама попросит prompt:

```text
Введите prompt для Lua-скрипта:
```

Именно этот способ лучше использовать для длинных русскоязычных prompt в Windows `cmd`, чтобы не упираться в кавычки и кодировку командной строки.

Если нужен запуск одной строкой:

```cmd
cd C:\Users\Admin\Desktop\TrueHach
py main.py --prompt "Write a simple Lua script that reads one line and prints its length."
```

### Если запускаешь из WSL

`lua` и `luacheck` из проекта уже подхватываются, но `LM Studio` по адресу `http://127.0.0.1:1234/v1` в WSL может быть недоступен.

Проверка:

```bash
python3 main.py --check-runtime --prompt test
curl http://127.0.0.1:1234/v1/models
```

Если `curl` не отвечает, запускай пайплайн из обычного Windows `cmd`, а не из WSL.

Система работает как пайплайн из нескольких агентов:

1. `parse_task` - разбирает пользовательский запрос и превращает его в структурированную спецификацию.
2. `plan_task` - строит план реализации, проверки и тестирования.
3. `generate_code` - генерирует Lua-код по спецификации и плану.
4. `execute_code` - запускает Lua-код и собирает результат выполнения.
5. `test_code` - генерирует и запускает тестовый сценарий.
6. `repair_code` - исправляет код по результатам выполнения и тестов.
7. `finalize_artifact` - собирает итоговый финальный JSON-артефакт после успешной валидации.

## Состояния графа

- `NEW_TASK`
- `PARSED`
- `PLANNED`
- `CODE_GENERATED`
- `EXECUTED`
- `TESTED`
- `REPAIR_NEEDED`
- `FINALIZED`
- `FAILED`

## Текущая архитектура

- Граф собирается в [graph.py](/mnt/c/Users/Admin/Desktop/TrueHach/graph.py)
- Состояние описано в [state.py](/mnt/c/Users/Admin/Desktop/TrueHach/state.py)
- Реестр и подмена версий агентов идут через [factory.py](/mnt/c/Users/Admin/Desktop/TrueHach/factory.py)
- Базовый LLM-клиент для LM Studio находится в [llm_client.py](/mnt/c/Users/Admin/Desktop/TrueHach/llm_client.py)
- Исполнение Lua, LuaJIT, `lupa` и `luacheck` собрано в [lua_runtime.py](/mnt/c/Users/Admin/Desktop/TrueHach/lua_runtime.py)
- CLI-входная точка находится в [main.py](/mnt/c/Users/Admin/Desktop/TrueHach/main.py)

Агенты лежат по папкам `agents/<role>/<version>.py`.  
Это позволяет добавлять новые версии логики без переписывания графа.

## Модель

По умолчанию проект ожидает локальную модель LM Studio по адресу:

```text
http://127.0.0.1:1234/v1
```

По умолчанию используется модель:

```text
yi-coder-9b-chat
```

Клиент использует OpenAI-compatible endpoint `/chat/completions`.

## Установка Python-зависимостей

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Для Windows:

```cmd
cd C:\Users\Admin\Desktop\TrueHach
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Содержимое [requirements.txt](/mnt/c/Users/Admin/Desktop/TrueHach/requirements.txt):

- `langgraph`
- `langchain-core`
- `typing-extensions`
- `lupa`

`lupa` нужна как Python fallback/runtime для исполнения Lua, если нет отдельного бинарника `lua` или `luajit`.

## Установка Lua runtime

Для реального исполнения нужен хотя бы один из вариантов:

1. `lua`
2. `luajit`
3. Python-пакет `lupa`

Для линтинга нужен:

1. `luacheck`

### Вариант 1. Системные пакеты

На Linux/WSL:

```bash
sudo apt update
sudo apt install -y lua5.4 luajit luarocks
sudo luarocks install luacheck
```

Если пакет `lua5.4` ставит бинарник не как `lua`, можно явно передать путь через `--lua-path`.

### Вариант 1b. Windows

Если проект запускается на обычном Windows, а не внутри WSL, самый простой путь такой:

1. Установить Python.
2. Установить Python-зависимости проекта.
3. Либо поставить `lua`/`luajit` и `luacheck`, либо использовать `lupa`.

#### Самый простой вариант для Windows: только через `lupa`

Если не хочется отдельно ставить `lua.exe`, можно использовать Python runtime:

```powershell
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py --prompt "Напиши Lua-скрипт" --lua-backend lupa
```

#### Вариант с `lua.exe` или `luajit.exe`

Если у тебя уже есть `lua.exe` или `luajit.exe`, добавь каталог с бинарником в `PATH` или передай путь явно:

```powershell
python main.py --prompt "Напиши Lua-скрипт" --lua-backend lua --lua-path "C:\Lua\lua.exe"
python main.py --prompt "Напиши Lua-скрипт" --lua-backend luajit --luajit-path "C:\LuaJIT\luajit.exe"
```

Проверить, что Windows видит бинарники:

```powershell
where lua
where luajit
where luacheck
```

Если `luacheck` установлен отдельно, можно тоже передать путь явно:

```powershell
python main.py --prompt "Напиши Lua-скрипт" --lua-backend lua --lua-path "C:\Lua\lua.exe" --luacheck-path "C:\LuaRocks\luacheck.bat"
```

#### Как поставить `luacheck` на Windows

Обычно `luacheck` ставят через `LuaRocks`. После установки `LuaRocks` команда выглядит так:

```powershell
luarocks install luacheck
```

Если `luarocks` не прописан в `PATH`, можно использовать полный путь до `luarocks.bat`.

#### Если `luarocks install luacheck` падает на `x86_64-w64-mingw32-gcc`

Типичный лог выглядит так:

```text
"x86_64-w64-mingw32-gcc" не является внутренней или внешней командой
```

Это значит, что `LuaRocks` пытается собрать зависимость `luafilesystem` из исходников, но в системе нет GCC toolchain для Windows.

Что нужно сделать:

1. Установить MSYS2.
2. Установить через MSYS2 пакет GCC для Windows.
3. Добавить каталог с GCC в `PATH`.
4. Повторить установку `luacheck`.

Пример рабочего пути:

1. Установить MSYS2 в `C:\msys64`.
2. Открыть терминал `MSYS2 UCRT64`.
3. Выполнить:

```bash
pacman -Syu
pacman -S --needed mingw-w64-ucrt-x86_64-gcc
```

4. Добавить в `PATH`:

```text
C:\msys64\ucrt64\bin
```

5. Проверить:

```powershell
gcc --version
where gcc
```

6. После этого снова запустить:

```powershell
luarocks install luacheck
```

Если твоя сборка LuaRocks ожидает именно `x86_64-w64-mingw32-gcc`, это, как правило, означает, что ей нужен MinGW-w64 toolchain. В большинстве случаев установка GCC через MSYS2 решает проблему. Это вывод по симптомам ошибки и типичному поведению LuaRocks на Windows.

#### Адрес LM Studio на Windows

Если проект запускается не внутри Docker/контейнера, а прямо в Windows, локальный LM Studio обычно удобнее указывать так:

```powershell
python main.py --prompt "Напиши Lua-скрипт" --base-url "http://127.0.0.1:1234/v1"
```

Если проект запускается из контейнера и LM Studio работает на хост-машине Windows, тогда `host.docker.internal` подходит:

```powershell
python main.py --prompt "Напиши Lua-скрипт" --base-url "http://host.docker.internal:1234/v1"
```

### Вариант 2. Сборка Lua из исходников, которые уже лежат в проекте

В репозитории уже есть каталог `lua-5.5.0/`.

Пример для Linux/WSL:

```bash
cd lua-5.5.0
make linux
```

После этого можно запускать проект так:

```bash
python3 main.py --prompt "Напиши Lua-скрипт" --lua-backend lua --lua-path ./lua-5.5.0/src/lua
```

### Вариант 3. Только через `lupa`

Если CLI-бинарников нет, но установлен `lupa`, можно принудительно выбрать Python runtime:

```bash
python3 main.py --prompt "Напиши Lua-скрипт" --lua-backend lupa
```

## Проверка runtime

Проверить, что видит система:

```bash
python3 main.py --check-runtime --prompt test
```

Для Windows:

```cmd
py main.py --check-runtime --prompt test
```

Пример ожидаемого вывода:

```json
{
  "backend_preference": "auto",
  "selected_backend": "lua",
  "lua_path": "/usr/bin/lua",
  "luajit_path": null,
  "linter": "/usr/bin/luacheck",
  "lupa_available": true,
  "ready": true
}
```

## Запуск

Простой запуск:

```bash
python3 main.py --prompt "Напиши Lua-модуль с функцией add(a, b)"
```

Для Windows `cmd` лучше так:

```cmd
py main.py
```

Если нужен prompt прямо в команде:

```cmd
py main.py --prompt "Write a simple Lua script that reads one line and prints its length."
```

Для длинных русских prompt в Windows `cmd` удобнее не передавать `--prompt`, а вставлять текст после запуска `py main.py`.

С ручным выбором backend:

```bash
python3 main.py --prompt "Напиши Lua-модуль с функцией add(a, b)" --lua-backend luajit
python3 main.py --prompt "Напиши Lua-модуль с функцией add(a, b)" --lua-backend lua
python3 main.py --prompt "Напиши Lua-модуль с функцией add(a, b)" --lua-backend lupa
```

С явными путями:

```bash
python3 main.py \
  --prompt "Напиши Lua-модуль с функцией add(a, b)" \
  --lua-backend lua \
  --lua-path /usr/bin/lua \
  --luacheck-path /usr/local/bin/luacheck
```

Показать полное состояние графа:

```bash
python3 main.py --prompt "Напиши Lua-модуль с функцией add(a, b)" --show-state
```

Windows:

```cmd
py main.py --show-state
```

Посмотреть доступные версии агентов:

```bash
python3 main.py --list-versions
```

## Переключение версий агентов

Каждую роль можно переключать независимо.

Пример:

```bash
python3 main.py \
  --prompt "Напиши Lua-модуль с функцией add(a, b)" \
  --agent-version parse_task=v1 \
  --agent-version generate_code=v1 \
  --agent-version repair_code=v1
```

Также это можно делать через переменные окружения:

```bash
export GENERATE_CODE_VERSION=v1
export REPAIR_CODE_VERSION=v1
python3 main.py --prompt "Напиши Lua-модуль"
```

## Переменные окружения

### LM Studio

```bash
export LM_STUDIO_BASE_URL=http://127.0.0.1:1234/v1
export LM_STUDIO_MODEL=yi-coder-9b-chat
export LM_STUDIO_API_KEY=lm-studio
export LM_STUDIO_TIMEOUT=120
export LM_STUDIO_TEMPERATURE=0.2
```

### Runtime

```bash
export LUA_BACKEND=auto
export LUA_PATH=/usr/bin/lua
export LUAJIT_PATH=/usr/bin/luajit
export LUACHECK_PATH=/usr/local/bin/luacheck
export LUA_EXEC_TIMEOUT=15
export MAX_ATTEMPTS=3
export ARTIFACTS_DIR=artifacts
```

## Что возвращает пайплайн

После успешного завершения граф возвращает структурированный `final_artifact` в состоянии:

- `task_goal`
- `lua_code`
- `implementation_summary`
- `validation_summary`
- `usage_notes`
- `limitations`

CLI при успешном запуске печатает:

- итоговый `Status`
- краткую формулировку задачи
- summary по execution
- summary по tests

## Как сейчас выбирается runtime

Если задан `LUA_BACKEND=auto`, то порядок такой:

1. `lua`
2. `luajit`
3. `lupa`

Если backend выбран явно, система будет использовать только его.

## Ограничения текущей реализации

- Для LLM используется прямой HTTP-клиент к LM Studio, без `langchain-openai`.
- FastAPI в проект пока не добавлен.
- Качество генерации и ремонта зависит от доступности локальной модели в `LM Studio`.
- Если ни `lua`, ни `luajit`, ни `lupa` недоступны, этапы `execute_code` и `test_code` завершатся ошибкой.
- Если запускать из WSL, `LM Studio` на `127.0.0.1:1234` может быть недоступен, даже если в Windows он работает нормально.
