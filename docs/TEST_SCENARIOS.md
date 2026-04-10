# "Бизнес-сценарии и проверки"

# Test scenarios

## 1. New Lua without explicit path
### Prompt
`Создай Lua-скрипт калькулятора с консольным вводом`

### Expected path behavior
- если в чате ещё нет active target, система строит fallback slug target в текущем workspace root

### Expected pipeline
`resolve_target -> route_intent(create) -> generate -> validate -> verify -> save -> respond`

### Pass criteria
- код сгенерирован
- файл сохранён на диск
- `/path` показывает fallback target

## 2. New Lua in explicit directory
### Prompt
`Создай текстовую игру в папке C:\Work\LuaProjects`

### Expected path behavior
- система создаёт slug-based подпапку внутри `C:\Work\LuaProjects`
- внутри неё создаётся итоговый `.lua` файл

### Expected pipeline
`resolve_target(directory) -> route_intent(create) -> generate -> validate -> verify -> save -> respond`

### Pass criteria
- нужная директория создана
- итоговый `.lua` файл создан внутри неё
- ответ показывает фактический путь сохранения

## 3. Create or update explicit Lua file by path
### Prompt
`Сделай скрипт заметок в C:\Work\LuaProjects\notes_app.lua`

### Expected path behavior
- система использует именно указанный `.lua` path
- если файл уже существует, его содержимое доступно для refine/inspect flow

### Expected pipeline
`resolve_target(file) -> route_intent(create|change) -> generate|refine -> validate -> verify -> save -> respond`

### Pass criteria
- запись идёт именно в named file
- `/path` возвращает этот же путь

## 4. Refine active target in same chat
### Prompt
1. `Создай калькулятор в C:\Work\LuaProjects\calc.lua`
2. `Добавь историю последних операций`

### Expected path behavior
- второй prompt переиспользует active target из того же чата

### Expected pipeline
- turn 1: `resolve_target -> create -> generate -> validate -> verify -> save -> respond`
- turn 2: `resolve_target(active target) -> change -> refine -> validate -> verify|fix -> save -> respond`

### Pass criteria
- второй turn не теряет active target
- изменения сохраняются в тот же файл

## 5. Failed validation triggers fix loop
### Prompt
`Создай интерактивный Lua-скрипт с несколькими функциями и обработкой ввода`

### Expected pipeline
`resolve_target -> create -> generate -> validate(fail) -> fix -> validate -> ...`

### Pass criteria
- минимум одна fix iteration происходит при диагностике ошибки
- если валидация в итоге не пройдена, файл не записывается как финальный результат
- пользователь получает diagnostics в ответе

## 6. Question or inspect without file rewrite
### Prompt
1. `Создай игру в C:\Work\LuaProjects\game.lua`
2. `Объясни, как работает этот код`

### Expected pipeline
- turn 1: create/save flow
- turn 2: `resolve_target(active target) -> route_intent(inspect|question) -> answer -> end`

### Pass criteria
- второй turn возвращает текстовый ответ
- файл не перезаписывается

## 7. Command-level checks
### Commands
- `/path`
- `/status`
- `/code`
- `/prompt`
- `/retry`

### Pass criteria
- `/path` показывает active target и workspace
- `/status` показывает current task, target и last save
- `/code` показывает текущий Lua-код
- `/prompt` показывает base prompt и accumulated change requests
- `/retry` повторно гоняет pipeline для текущего кода
